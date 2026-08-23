"""
Telegram control-бот керування (Bot API, aiogram) — окремий бот-акаунт
(створюється через @BotFather), працює паралельно з listener-клієнтом
на Telethon у main.py в тому самому asyncio event loop.

Доступ — дворівневий:
  - "admin"  — повний доступ: усі команди, включно з /stop, /setlimit,
    керуванням списком користувачів. TG_OWNER_USER_ID з .env ЗАВЖДИ admin
    (жорстко, не зберігається в runtime_state.json, не можна видалити
    через бота) — інші admin/user додаються через кнопку "👥 Користувачі"
    і зберігаються в core/runtime_state.py.
  - "user"   — лише перегляд: /status, /balance, /positions, /history.

Будь-хто поза цими двома списками мовчки ігнорується (є лише warning у лог,
без відповіді в чат) — щоб стороння людина, яка випадково знайде бота, навіть
не бачила, що він на щось реагує. Фільтри застосовано ОКРЕМО на текстові
повідомлення (router.message) і на inline-кнопки (router.callback_query) —
це дві різні черги подій в aiogram, і на кожен хендлер фільтр вказується
явно (а не як один спільний router.message.filter(...)), бо різні хендлери
вимагають різного рівня доступу.

СКЕПТИЧНИЙ КОМЕНТАР: єдина авторизація тут — Telegram user_id. Якщо власник
чи доданий адмін колись втратить контроль над своїм Telegram-акаунтом
(злом/SIM-swap), той, хто його перехопить, отримає й контроль над ботом
(/stop, /setlimit, навіть додавання нових адмінів). Для реальних грошей варто
розглянути додатковий фактор, але це свідомо не реалізовано зараз.

Кнопкове меню — додатковий, паралельний спосіб виклику команд, НЕ заміна
текстових команд: /status, /setlimit НАЗВА значення тощо продовжують
працювати як і раніше, незалежно від кнопок.
"""
import datetime as dt
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import func

from core import runtime_state
from core.config import settings, LIMIT_FIELDS, get_limit, is_limit_overridden
from core.storage import get_session, SignalLog, Trade, TEST_TOKEN_SYMBOL
from core.wallet import get_wallet_balance
from core.formatting import display_token_symbol

logger = logging.getLogger(__name__)

router = Router(name="control_bot")


def get_role(user_id: "int | None") -> "str | None":
    """
    'admin' / 'user' / None (немає доступу). TG_OWNER_USER_ID з .env — завжди
    'admin', перевіряється тут, а не зберігається в runtime_state.json.
    """
    if user_id is None:
        return None
    if settings.tg_owner_user_id and user_id == settings.tg_owner_user_id:
        return "admin"
    return runtime_state.get_user_role(user_id)


class IsAllowed(BaseFilter):
    """Пропускає будь-кого з роллю (admin ЧИ user) — для read-only команд."""

    async def __call__(self, event) -> bool:
        user_id = event.from_user.id if getattr(event, "from_user", None) else None
        if get_role(user_id) is None:
            logger.warning(f"Control-бот: подія від user_id={user_id} проігнорована (немає доступу)")
            return False
        return True


class IsAdmin(BaseFilter):
    """Пропускає лише admin — для команд, що змінюють стан бота (/stop, /setlimit, керування користувачами)."""

    async def __call__(self, event) -> bool:
        user_id = event.from_user.id if getattr(event, "from_user", None) else None
        role = get_role(user_id)
        if role != "admin":
            logger.warning(f"Control-бот: адмін-команда від user_id={user_id} (роль={role}) відхилена")
            return False
        return True


is_allowed = IsAllowed()
is_admin = IsAdmin()

# П.2 аудиту: скільки хвилин Trade зі status="pending" вважається "застряглим"
# для лічильника в /status — див. cmd_status().
STUCK_PENDING_THRESHOLD_MINUTES = 5


# --- Reply-клавіатури: адмін бачить повний набір, user — лише перегляд ---
USER_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="💰 Баланс")],
        [KeyboardButton(text="📈 Позиції"), KeyboardButton(text="📜 Історія")],
        [KeyboardButton(text="📊 Статистика")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="💰 Баланс")],
        [KeyboardButton(text="📈 Позиції"), KeyboardButton(text="📜 Історія")],
        [KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="⏸ Стоп"), KeyboardButton(text="▶️ Старт")],
        [KeyboardButton(text="⚙️ Ліміти"), KeyboardButton(text="👥 Користувачі")],
        [KeyboardButton(text="🧪 Тест")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def keyboard_for(user_id: "int | None") -> ReplyKeyboardMarkup:
    return ADMIN_KEYBOARD if get_role(user_id) == "admin" else USER_KEYBOARD


class SetLimitStates(StatesGroup):
    waiting_for_value = State()


class UserMgmtStates(StatesGroup):
    waiting_for_new_user_id = State()


def _today_start() -> dt.datetime:
    return dt.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


# --- ФОРМАЛЬНІ FSM-ХЕНДЛЕРИ — РЕЄСТРУЮТЬСЯ ПЕРШИМИ серед message-хендлерів ---
# aiogram перевіряє хендлери в порядку реєстрації і зупиняється на першому
# збігу (TelegramEventObserver.trigger). Якби кнопкові F.text-хендлери нижче
# були зареєстровані раніше, натискання reply-кнопки під час очікування
# вводу "перехопило" б повідомлення ще ДО того, як стан-фільтр встиг би його
# побачити — FSM завис би назавжди, а команда відпрацювала б як звичайно,
# заплутуючи користувача. Обидва FSM тут не конфліктують між собою: в
# кожного власника чату — окремий FSM-контекст, і активний тільки один стан.

@router.message(SetLimitStates.waiting_for_value, is_admin)
async def fsm_setlimit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    env_name = data.get("limit_name")
    if not env_name or env_name not in LIMIT_FIELDS:
        await state.clear()
        await message.answer(
            "Внутрішня помилка стану, спробуй ще раз через ⚙️ Ліміти.",
            reply_markup=keyboard_for(message.from_user.id),
        )
        return

    raw_value = (message.text or "").strip()

    if raw_value.lower() == "default":
        cleared = runtime_state.clear_limit_override(env_name)
        await state.clear()
        msg = "скинуто до значення з .env" if cleared else "не було перевизначено — і так з .env"
        await message.answer(
            f"✅ {env_name}: {msg} ({get_limit(env_name)})",
            reply_markup=keyboard_for(message.from_user.id),
        )
        return

    _, value_type, unit = LIMIT_FIELDS[env_name]
    try:
        # Валідація — той самий type-cast, що й у текстовій команді /setlimit
        # (float/int з LIMIT_FIELDS). У проєкті НЕМАЄ окремої перевірки "розумного
        # діапазону" значень (напр. min/max меж) ні в config.py, ні в risk_manager.py —
        # навмисно не вигадуємо нову тут, щоб поведінка кнопки й тексту лишались
        # ідентичними. Якщо захочеш range-перевірку — це окрема задача.
        value = value_type(raw_value)
    except ValueError:
        await message.answer(
            f"Не вдалось перетворити '{raw_value}' у {value_type.__name__}. Спробуй ще раз, "
            f"або натисни 🔙 Скасувати на попередньому повідомленні.",
            reply_markup=_cancel_only_keyboard("setlimit:cancel"),
        )
        return  # лишаємось в тому ж FSM-стані, даємо ввести ще раз

    old_value = get_limit(env_name)
    runtime_state.set_limit_override(env_name, value)
    await state.clear()
    await message.answer(
        f"✅ {env_name} змінено: {old_value} → {value} {unit}\nДіє одразу, без перезапуску бота.",
        reply_markup=keyboard_for(message.from_user.id),
    )


@router.message(UserMgmtStates.waiting_for_new_user_id, is_admin)
async def fsm_add_user_value(message: Message, state: FSMContext):
    data = await state.get_data()
    role = data.get("new_user_role")
    if role not in ("admin", "user"):
        await state.clear()
        await message.answer(
            "Внутрішня помилка стану, спробуй ще раз через 👥 Користувачі.",
            reply_markup=keyboard_for(message.from_user.id),
        )
        return

    raw = (message.text or "").strip()
    try:
        new_id = int(raw)
        if new_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            f"'{raw}' не схоже на числовий Telegram user_id. Дізнатись id — напр. через "
            f"@userinfobot. Спробуй ще раз, або натисни 🔙 Скасувати.",
            reply_markup=_cancel_only_keyboard("users:cancel"),
        )
        return

    if settings.tg_owner_user_id and new_id == settings.tg_owner_user_id:
        await state.clear()
        await message.answer(
            "Це вже власник бота (завжди admin) — додавати не треба.",
            reply_markup=keyboard_for(message.from_user.id),
        )
        return

    runtime_state.add_user(new_id, role)
    await state.clear()
    role_label = "адміна" if role == "admin" else "користувача (тільки перегляд)"
    await message.answer(
        f"✅ user_id={new_id} доданий як {role_label}.",
        reply_markup=keyboard_for(message.from_user.id),
    )


@router.message(Command("status"), is_allowed)
@router.message(F.text == "📊 Статус", is_allowed)
async def cmd_status(message: Message):
    session = get_session()
    try:
        start = _today_start()
        signals_today = session.query(SignalLog).filter(SignalLog.created_at >= start).count()
        executed_today = session.query(SignalLog).filter(
            SignalLog.created_at >= start, SignalLog.executed.is_(True)
        ).count()
        rejected_today = session.query(SignalLog).filter(
            SignalLog.created_at >= start, SignalLog.rejection_reason.isnot(None)
        ).count()
        # Рахуємо по структурованій колонці chain, а не по тексту rejection_reason
        # (крихкіше було б звірятись з конкретним форматуванням фрази в main.py) —
        # is_signal=True І chain задано, але не "solana" = LLM визнав сигнал, але
        # мережа поки не підтримується виконанням (тільки Solana).
        unsupported_chain_today = session.query(SignalLog).filter(
            SignalLog.created_at >= start,
            SignalLog.is_signal.is_(True),
            SignalLog.chain.isnot(None),
            SignalLog.chain != "solana",
        ).count()

        mode = "🧪 DRY RUN (без реальних угод)" if settings.dry_run else "🔴 LIVE (реальні гроші)"
        pause_state = "⏸️ НА ПАУЗІ (/start щоб відновити)" if runtime_state.is_paused() else "▶️ активна"

        # Застряглий pending — Trade зі status="pending" (П.2, core/main.py і
        # core/position_monitor.py), який досі НЕ оновився до confirmed/failed
        # довше STUCK_PENDING_THRESHOLD_MINUTES. У штатному режимі pending
        # живе частки секунди (між створенням рядка і завершенням свопу) —
        # якщо він старший за поріг, це ознака, що процес впав саме в цьому
        # вікні (реальна транзакція могла піти в мережу, а результат не
        # записався) і потрібне ручне втручання.
        stuck_pending_before = dt.datetime.utcnow() - dt.timedelta(minutes=STUCK_PENDING_THRESHOLD_MINUTES)
        stuck_pending_count = session.query(Trade).filter(
            Trade.status == "pending", Trade.created_at < stuck_pending_before
        ).count()
        pending_line = f"\n⚠️ Застряглих pending-угод: {stuck_pending_count}" if stuck_pending_count > 0 else ""

        await message.answer(
            "📊 <b>Статус бота</b>\n"
            f"Режим: {mode}\n"
            f"Торгівля: {pause_state}\n"
            f"Сигналів сьогодні: {signals_today}\n"
            f"Виконано угод: {executed_today}\n"
            f"Відхилено: {rejected_today}\n"
            f"Відхилено через непідтримувану мережу: {unsupported_chain_today}"
            f"{pending_line}",
            parse_mode="HTML",
        )
    finally:
        session.close()


@router.message(Command("balance"), is_allowed)
@router.message(F.text == "💰 Баланс", is_allowed)
async def cmd_balance(message: Message):
    session = get_session()
    try:
        open_positions_usd = _open_positions_total_usd(session)
    finally:
        session.close()

    balance = get_wallet_balance()
    usdt_line = f"${balance.usdt_balance:,.2f}" if balance.usdt_balance is not None else "н/д"
    sol_line = f"{balance.sol_balance:.4f} SOL" if balance.sol_balance is not None else "н/д"
    gas_marker = " ⚠️ МАЛО НА ГАЗ" if balance.low_gas_warning else ""

    await message.answer(
        "💰 <b>Баланс гаманця</b>\n"
        f"USDT (торговий капітал): {usdt_line}\n"
        f"SOL (резерв на газ): {sol_line}{gas_marker}\n"
        f"У відкритих позиціях: ≈ ${open_positions_usd:,.2f}\n"
        f"{'⚠️ ' + balance.note if balance.note else ''}",
        parse_mode="HTML",
    )


def _open_positions_total_usd(session) -> float:
    # .isnot(TEST_TOKEN_SYMBOL), не "!=" — token_symbol часто NULL, а "!="
    # в SQL мовчки виключив би й ці рядки разом з тестовими (див. коментар
    # в core/stats.py:_compute_trade_stats).
    buys = session.query(func.sum(Trade.amount_usd)).filter(
        Trade.action == "buy", Trade.status == "confirmed",
        Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL),
    ).scalar() or 0.0
    sells = session.query(func.sum(Trade.amount_usd)).filter(
        Trade.action == "sell", Trade.status == "confirmed",
        Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL),
    ).scalar() or 0.0
    return max(buys - sells, 0.0)


@router.message(Command("positions"), is_allowed)
@router.message(F.text == "📈 Позиції", is_allowed)
async def cmd_positions(message: Message):
    session = get_session()
    try:
        # Групування "відкритих" позицій ПО КОНТРАКТУ (не по token_symbol!) —
        # token_symbol часто NULL (SignalParser не завжди має звідки взяти
        # тікер, див. core/formatting.py:display_token_symbol), і групування
        # по token_symbol раніше зливало ВСІ токени без тікера в одну спільну
        # групу "None", помилково підсумовуючи USD непов'язаних позицій разом.
        # func.max(token_symbol) бере тікер там, де він є, для показу.
        # Сума buy мінус сума sell в USD — НЕ реальна кількість токенів і НЕ
        # поточний PnL, просто скільки USD ще "в грі" по позиції станом на
        # ціни купівлі. Реальний PnL вимагає окремого моніторингу поточної
        # ціни (не реалізовано, див. README).
        # Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL) — виключає тестові
        # угоди від кнопки "🧪 Тест" (isnot(), не "!=", щоб не зачепити й
        # реальні рядки з token_symbol=NULL, див. коментар в core/stats.py).
        rows = session.query(
            Trade.contract_address,
            func.max(Trade.token_symbol),
            Trade.action,
            func.sum(Trade.amount_usd),
        ).filter(
            Trade.status == "confirmed",
            Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL),
        ).group_by(Trade.contract_address, Trade.action).all()

        net_by_contract: dict[str, list] = {}  # contract -> [net_usd, token_symbol]
        for contract_address, token_symbol, action, total in rows:
            entry = net_by_contract.setdefault(contract_address, [0.0, token_symbol])
            entry[0] += total if action == "buy" else -total
            if token_symbol:
                entry[1] = token_symbol

        open_positions = {c: v for c, v in net_by_contract.items() if v[0] > 0.01}

        if not open_positions:
            await message.answer("📭 Немає відкритих позицій.")
            return

        lines = ["📈 <b>Відкриті позиції</b> (PnL н/д — без моніторингу ціни в реальному часі)"]
        for contract_address, (usd, token_symbol) in sorted(open_positions.items(), key=lambda x: -x[1][0]):
            label = display_token_symbol(token_symbol, contract_address)
            lines.append(f"• {label}: ≈ ${usd:,.2f}")
        await message.answer("\n".join(lines), parse_mode="HTML")
    finally:
        session.close()


async def _send_history(message: Message, limit: int):
    session = get_session()
    try:
        # .isnot(TEST_TOKEN_SYMBOL) — виключає тестові угоди від кнопки
        # "🧪 Тест" (див. коментар в core/stats.py щодо чому isnot(), не "!=").
        trades = session.query(Trade).filter(
            Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL)
        ).order_by(Trade.created_at.desc()).limit(limit).all()
        if not trades:
            await message.answer("Історія угод порожня.")
            return

        lines = [f"🕓 <b>Останні {len(trades)} угод</b>"]
        for t in trades:
            prefix = "🧪" if t.dry_run else "💵"
            ts = t.created_at.strftime("%Y-%m-%d %H:%M")
            label = display_token_symbol(t.token_symbol, t.contract_address)
            lines.append(
                f"{prefix} {ts} | {t.action.upper()} {label} "
                f"${t.amount_usd:.2f} | {t.status}"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")
    finally:
        session.close()


@router.message(Command("history"), is_allowed)
async def cmd_history(message: Message, command: CommandObject):
    limit = 10
    if command.args:
        try:
            limit = max(1, min(50, int(command.args.strip())))
        except ValueError:
            await message.answer("Використання: /history [число від 1 до 50]")
            return
    await _send_history(message, limit)


@router.message(F.text == "📜 Історія", is_allowed)
async def cmd_history_button(message: Message):
    # Кнопка не передає аргументів — завжди дефолтні останні 10, як /history без параметра.
    await _send_history(message, 10)


@router.message(Command("stop"), is_admin)
@router.message(F.text == "⏸ Стоп", is_admin)
async def cmd_stop(message: Message):
    runtime_state.set_paused(True)
    await message.answer(
        "⏸️ Торгівлю призупинено. Бот продовжує слухати канал і логувати сигнали, "
        "але НЕ виконуватиме жодних свопів, поки не викличеш /start.",
        reply_markup=keyboard_for(message.from_user.id),
    )


@router.message(Command("start"), is_allowed)
@router.message(F.text == "▶️ Старт", is_admin)
async def cmd_start(message: Message):
    """
    /start (текстова команда) доступна ВСІМ з роллю (admin і user) — це
    типова точка входу в Telegram-боти (показує клавіатуру після приєднання),
    а не команда паузи. Кнопка "▶️ Старт" — окремий admin-only хендлер нижче
    в тому ж handler-списку: у user'а її й нема на клавіатурі (USER_KEYBOARD),
    але текстову команду "/start" він теж може ввести — тоді знімати паузу
    вона не повинна (лише показати клавіатуру), тому саму дію "зняти паузу"
    виконує ТІЛЬКИ якщо викликач — admin.
    """
    role = get_role(message.from_user.id)
    if role == "admin":
        runtime_state.set_paused(False)
        await message.answer("▶️ Торгівлю відновлено.", reply_markup=keyboard_for(message.from_user.id))
    else:
        await message.answer(
            "👋 Вітаю! Доступ: перегляд (read-only).",
            reply_markup=keyboard_for(message.from_user.id),
        )


def _limits_inline_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for env_name in LIMIT_FIELDS:
        value = get_limit(env_name)
        rows.append([InlineKeyboardButton(text=f"{env_name}: {value}", callback_data=f"setlimit:{env_name}")])
    rows.append([InlineKeyboardButton(text="🔙 Скасувати", callback_data="setlimit:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_only_keyboard(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Скасувати", callback_data=callback_data)]])


@router.message(Command("limits"), is_admin)
@router.message(F.text == "⚙️ Ліміти", is_admin)
async def cmd_limits(message: Message):
    lines = ["⚙️ <b>Поточні ризик-ліміти</b>"]
    for env_name, (_, _, unit) in LIMIT_FIELDS.items():
        value = get_limit(env_name)
        marker = " (перевизначено /setlimit)" if is_limit_overridden(env_name) else ""
        lines.append(f"• {env_name} = {value} {unit}{marker}")
    lines.append("\nНатисни кнопку нижче, щоб змінити конкретний ліміт, або текстом:")
    lines.append("/setlimit НАЗВА значення (напр. /setlimit MAX_OPEN_POSITIONS 5)")
    lines.append("Скинути до .env: /setlimit НАЗВА default")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=_limits_inline_keyboard())


@router.callback_query(F.data.startswith("setlimit:"), is_admin)
async def cb_setlimit_pick(callback: CallbackQuery, state: FSMContext):
    """
    Тап на inline-кнопку ліміту → FSM-стан "чекаємо число" для цього конкретного
    ліміту. Тап на "🔙 Скасувати" (доступна і в першому списку, і в наступному
    промпті "введи значення") виходить зі стану без жодних змін.
    """
    action = callback.data.split(":", 1)[1]

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("Скасовано, ліміти не змінено.")
        await callback.answer()
        return

    env_name = action
    if env_name not in LIMIT_FIELDS:
        await callback.answer("Невідомий ліміт", show_alert=True)
        return

    current = get_limit(env_name)
    await state.update_data(limit_name=env_name)
    await state.set_state(SetLimitStates.waiting_for_value)
    await callback.message.edit_text(
        f"Введи нове значення для {env_name} (поточне: {current}):",
        reply_markup=_cancel_only_keyboard("setlimit:cancel"),
    )
    await callback.answer()


@router.message(Command("setlimit"), is_admin)
async def cmd_setlimit(message: Message, command: CommandObject):
    if not command.args or len(command.args.split()) != 2:
        await message.answer(
            "Використання: /setlimit НАЗВА значення\n"
            "Приклад: /setlimit MAX_OPEN_POSITIONS 5\n"
            "Скинути до .env: /setlimit MAX_OPEN_POSITIONS default\n"
            "Список назв: /limits"
        )
        return

    name, raw_value = command.args.split()
    name = name.strip().upper()

    if name not in LIMIT_FIELDS:
        await message.answer(f"Невідомий ліміт: {name}\nСписок назв: /limits")
        return

    if raw_value.strip().lower() == "default":
        cleared = runtime_state.clear_limit_override(name)
        msg = "скинуто до значення з .env" if cleared else "не було перевизначено — і так з .env"
        await message.answer(f"✅ {name}: {msg} ({get_limit(name)})")
        return

    _, value_type, unit = LIMIT_FIELDS[name]
    try:
        value = value_type(raw_value)
    except ValueError:
        await message.answer(f"Не вдалось перетворити '{raw_value}' у {value_type.__name__}")
        return

    old_value = get_limit(name)
    runtime_state.set_limit_override(name, value)
    await message.answer(
        f"✅ {name}: {old_value} → {value} {unit}\n"
        f"Діє одразу, без перезапуску бота."
    )


def _users_inline_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=f"👑 {settings.tg_owner_user_id} — власник (admin, незмінний)",
        callback_data="users:noop",
    )]]
    for uid_str, role in runtime_state.get_users().items():
        icon = "🛠" if role == "admin" else "👁"
        rows.append([
            InlineKeyboardButton(text=f"{icon} {uid_str} ({role})", callback_data="users:noop"),
            InlineKeyboardButton(text="❌ Видалити", callback_data=f"users:remove:{uid_str}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Додати адміна", callback_data="users:add:admin")])
    rows.append([InlineKeyboardButton(text="➕ Додати користувача (перегляд)", callback_data="users:add:user")])
    rows.append([InlineKeyboardButton(text="🔙 Закрити", callback_data="users:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("users"), is_admin)
@router.message(F.text == "👥 Користувачі", is_admin)
async def cmd_users(message: Message):
    await message.answer(
        "👥 <b>Користувачі control-бота</b>\n"
        "🛠 admin — повний доступ (як власник), 👁 user — лише перегляд.",
        parse_mode="HTML",
        reply_markup=_users_inline_keyboard(),
    )


@router.callback_query(F.data.startswith("users:"), is_admin)
async def cb_users(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    action = parts[1]

    if action == "noop":
        await callback.answer()
        return

    if action == "close":
        await callback.message.edit_text("Закрито.")
        await callback.answer()
        return

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("Скасовано.")
        await callback.answer()
        return

    if action == "remove":
        target_id = parts[2]
        removed = runtime_state.remove_user(int(target_id))
        await callback.message.edit_text(
            f"{'✅ Видалено' if removed else 'Вже не було в списку'}: user_id={target_id}"
        )
        await callback.answer()
        return

    if action == "add":
        role = parts[2]  # "admin" | "user"
        if role not in ("admin", "user"):
            await callback.answer("Невідома роль", show_alert=True)
            return
        await state.update_data(new_user_role=role)
        await state.set_state(UserMgmtStates.waiting_for_new_user_id)
        role_label = "адміна" if role == "admin" else "користувача (тільки перегляд)"
        await callback.message.edit_text(
            f"Введи Telegram user_id нового {role_label} (число — дізнатись, напр., через @userinfobot):",
            reply_markup=_cancel_only_keyboard("users:cancel"),
        )
        await callback.answer()
        return

    await callback.answer()


@router.message(Command("test"), is_admin)
@router.message(F.text == "🧪 Тест", is_admin)
async def cmd_test(message: Message):
    """
    Повний тестовий прогін торгового пайплайну + ladder TP/SL — ГАРАНТОВАНО
    симуляція, незалежно від DRY_RUN у .env (див. core/self_test.py і
    force_dry_run у core/okx_dex_client.py:OKXDexClient.execute_swap()).

    core.self_test імпортується ЛОКАЛЬНО (не на початку файлу) навмисно:
    core.self_test імпортує core.position_monitor, а core.position_monitor
    імпортує core.control_bot (цей файл) для notify_owner() — імпорт
    self_test на рівні модуля тут створив би цикл control_bot -> self_test ->
    position_monitor -> control_bot. Локальний імпорт всередині хендлера
    спрацьовує без проблем, бо на момент першого натискання кнопки всі три
    модулі вже повністю завантажені (main.py імпортує їх усі при старті).
    """
    from core.self_test import run_buy_signal_test, run_ladder_test

    await message.answer("🧪 Запускаю тестовий прогін (buy-сигнал)...")
    buy_report = await run_buy_signal_test()
    await message.answer("\n".join(buy_report), parse_mode="HTML")

    await message.answer("🧪 Запускаю тестовий прогін (ladder TP/SL)...")
    ladder_report = await run_ladder_test()
    await message.answer("\n".join(ladder_report), parse_mode="HTML")


def _stats_period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="День", callback_data="stats:day"),
        InlineKeyboardButton(text="Тиждень", callback_data="stats:week"),
        InlineKeyboardButton(text="Місяць", callback_data="stats:month"),
    ]])


@router.message(Command("stats"), is_allowed)
@router.message(F.text == "📊 Статистика", is_allowed)
async def cmd_stats(message: Message):
    await message.answer("📊 За який період?", reply_markup=_stats_period_keyboard())


@router.callback_query(F.data.startswith("stats:"), is_allowed)
async def cb_stats(callback: CallbackQuery):
    """
    core.stats імпортується ЛОКАЛЬНО (не на початку файлу) з тієї ж причини,
    що й core.self_test у cmd_test() вище: core.stats імпортує
    core.position_monitor, а core.position_monitor імпортує core.control_bot
    (цей файл) для notify_owner() — імпорт на рівні модуля тут створив би
    цикл control_bot -> stats -> position_monitor -> control_bot.
    """
    from core.stats import format_stats_report

    period_key = callback.data.split(":", 1)[1]
    await callback.answer()

    session = get_session()
    try:
        report = format_stats_report(session, period_key)
    finally:
        session.close()

    await callback.message.edit_text(report, parse_mode="HTML")


@router.message(Command("help"), is_allowed)
async def cmd_help(message: Message):
    role = get_role(message.from_user.id)
    lines = [
        "📋 <b>Команди</b>",
        "/status — статус бота і статистика за сьогодні",
        "/balance — баланс гаманця",
        "/positions — відкриті позиції",
        "/history [N] — останні N угод (default 10)",
        "/stats — статистика за день/тиждень/місяць (сигнали, PnL, ladder)",
    ]
    if role == "admin":
        lines += [
            "/stop — призупинити торгівлю",
            "/start — відновити торгівлю",
            "/limits — поточні ризик-ліміти (+ кнопки для зміни)",
            "/setlimit НАЗВА значення — змінити ліміт на льоту",
            "/users — керування користувачами control-бота",
            "/test — тестовий прогін пайплайну + ladder TP/SL (завжди симуляція)",
        ]
    lines.append(
        "\nКнопкове меню під полем вводу дублює основні команди — це паралельний "
        "спосіб виклику, текстові команди так само працюють."
    )
    await message.answer("\n".join(lines), parse_mode="HTML")


_bot: "Bot | None" = None


async def notify_owner(text: str):
    """
    Сповіщення власнику ЧЕРЕЗ CONTROL-БОТА (не через Telethon-listener і не
    через TG_NOTIFY_CHAT_ID) — окремий канал для подій, що стаються поза
    командним потоком, напр. автоматичне спрацювання ladder TP/SL з
    core/position_monitor.py. Свідомо відокремлено від сповіщень про сигнали
    з каналу (main.py notify()), щоб було видно, звідки прийшла подія.
    Йде ЛИШЕ власнику (TG_OWNER_USER_ID), не всім доданим адмінам — це
    свідомо вужче коло, ніж керування ботом, окрема задача, якщо знадобиться
    розсилка на всіх admin.
    """
    if _bot is None or not settings.tg_owner_user_id:
        logger.info(f"NOTIFY (control-бот не запущено): {text}")
        return
    try:
        await _bot.send_message(settings.tg_owner_user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Не вдалось надіслати сповіщення власнику через control-бота: {e}")


async def run_control_bot():
    global _bot
    if not settings.tg_bot_token or not settings.tg_owner_user_id:
        logger.warning(
            "TG_BOT_TOKEN / TG_OWNER_USER_ID не задані — control-бот НЕ запущено. "
            "Команди /status, /stop, /setlimit тощо недоступні."
        )
        return

    _bot = Bot(token=settings.tg_bot_token)
    # MemoryStorage — достатньо для FSM невеликої кількості admin/user
    # (кожен зі своїм окремим FSM-контекстом за user_id/chat_id).
    # aiogram і без явної вказівки за замовчуванням підставив би MemoryStorage,
    # але прописуємо явно, щоб вибір сховища не був прихованою деталлю.
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Control-бот запущено (aiogram, Bot API), доступ: власник + додані admin/user")
    await dp.start_polling(_bot)
