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
import asyncio
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

    # asyncio.to_thread — get_wallet_balance() робить синхронний Solana RPC-
    # виклик; без цього /balance блокував би control-бота (та спільний event
    # loop із ladder-монітором) на час запиту, той самий патерн, що вже
    # застосовано для мережевих викликів у main.py.
    balance = await asyncio.to_thread(get_wallet_balance)
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
    """
    На відміну від старої версії (групування по contract_address) — тепер
    показує ОКРЕМИЙ рядок на КОЖЕН відкритий buy-рядок (buy.id), а не
    згруповано по контракту. Причина: примусове закриття (❌ нижче) діє на
    конкретний buy_id, а групування по контракту робило б неможливим
    однозначно вказати, ЯКУ саме позицію закривати, якби по тому самому
    контракту колись існувало кілька окремих buy-рядків.
    """
    from core.position_monitor import remaining_amount, POSITION_EPSILON

    session = get_session()
    try:
        # Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL) — виключає тестові
        # угоди від кнопки "🧪 Тест" (isnot(), не "!=", щоб не зачепити й
        # реальні рядки з token_symbol=NULL, див. коментар в core/stats.py).
        open_buys = session.query(Trade).filter(
            Trade.action == "buy", Trade.status == "confirmed",
            Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL),
        ).order_by(Trade.created_at.desc()).all()

        rows_data = []
        for buy in open_buys:
            remaining = remaining_amount(session, buy)
            if remaining <= POSITION_EPSILON:
                continue
            # USD, пропорційний залишку (не весь buy.amount_usd) — якщо
            # частину вже продано по ladder TP/SL, показуємо лише те, що
            # реально ще "в грі", а не первинний розмір угоди.
            usd_in_position = 0.0
            if buy.amount_usd and buy.token_amount:
                usd_in_position = buy.amount_usd * (remaining / buy.token_amount)
            rows_data.append((buy, usd_in_position))

        if not rows_data:
            await message.answer("📭 Немає відкритих позицій.")
            return

        # PnL н/д — без моніторингу поточної ціни ТУТ (ladder-монітор рахує
        # це окремо для власних потреб, core/position_monitor.py).
        lines = ["📈 <b>Відкриті позиції</b> (PnL н/д — без моніторингу ціни в реальному часі)"]
        keyboard_rows = []
        is_admin_role = get_role(message.from_user.id) == "admin"
        for buy, usd in sorted(rows_data, key=lambda x: -x[1]):
            label = display_token_symbol(buy.token_symbol, buy.contract_address)
            lines.append(f"• {label}: ≈ ${usd:,.2f}")
            if is_admin_role:
                keyboard_rows.append([
                    InlineKeyboardButton(text=f"❌ Закрити {label}", callback_data=f"forceclose:{buy.id}")
                ])

        await message.answer(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None,
        )
    finally:
        session.close()


async def _force_close_position(buy_id: int) -> tuple[bool, str]:
    """
    Позначає buy-позицію примусово закритою: додає sell-рядок з
    close_reason="manual_force_close", amount_usd=0.0 (токен вважається
    непродаваним — власник підтвердив це вручну) і pnl_usd = -повна
    собівартість залишку (чесний повний збиток, а не пропуск чи хибний
    прибуток у статистиці). НІКОЛИ не звертається до OKX (жодного
    get_quote/execute_swap) — саме для сценарію, коли токен вже недоступний
    для реальної торгівлі (rug/делістинг), і автоматична ladder-логіка
    застрягла на price-divergence guard (core/position_monitor.py).

    Той самий per-позиція lock, що й execute_partial_sell() (core/
    position_monitor.py) — щоб не закрити позицію ОДНОЧАСНО з тим, як
    ladder/сигнал-sell вже продають її частку.
    """
    from core.position_monitor import remaining_amount, POSITION_EPSILON, _lock_for, _position_locks, _divergence_block_counts

    session = get_session()
    try:
        buy = session.get(Trade, buy_id)
        if not buy or buy.action != "buy" or buy.status != "confirmed":
            return False, "Позицію не знайдено, або вона не є відкритою buy-угодою."

        async with _lock_for(buy.id):
            remaining = remaining_amount(session, buy)
            if remaining <= POSITION_EPSILON:
                return False, "Позиція вже закрита (залишок 0) — нічого закривати."

            cost_per_raw_unit = (
                buy.amount_usd / buy.token_amount if buy.amount_usd and buy.token_amount else 0.0
            )
            loss_usd = cost_per_raw_unit * remaining

            sell_trade = Trade(
                action="sell",
                token_symbol=buy.token_symbol,
                contract_address=buy.contract_address,
                chain=buy.chain,
                amount_usd=0.0,
                pnl_usd=-loss_usd,
                token_amount=remaining,
                dry_run=buy.dry_run,
                status="confirmed",
                parent_trade_id=buy.id,
                close_reason="manual_force_close",
            )
            session.add(sell_trade)
            session.commit()

        # Позиція назавжди закрита — на відміну від звичайного відкритого
        # стану (де lock/лічильник може знадобитись багато разів наперед),
        # тут майбутніх звернень до цього buy_id більше не буде: наступний
        # цикл ladder-монітора вже не поверне її з _get_open_positions()
        # (remaining_amount() тепер 0). Прибираємо "сирітський" стан.
        _position_locks.pop(buy_id, None)
        _divergence_block_counts.pop(buy_id, None)

        label = display_token_symbol(buy.token_symbol, buy.contract_address)
        return True, (
            f"✅ Позицію {label} примусово закрито (manual_force_close). "
            f"Врахована як повний збиток ${loss_usd:.2f} у статистиці."
        )
    finally:
        session.close()


def _forceclose_confirm_keyboard(buy_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Так, закрити", callback_data=f"forceclose_confirm:{buy_id}"),
        InlineKeyboardButton(text="🔙 Скасувати", callback_data="forceclose_cancel"),
    ]])


@router.callback_query(F.data.startswith("forceclose:"), is_admin)
async def cb_forceclose_prompt(callback: CallbackQuery):
    buy_id = int(callback.data.split(":", 1)[1])
    session = get_session()
    try:
        buy = session.get(Trade, buy_id)
    finally:
        session.close()

    if not buy:
        await callback.answer("Позицію не знайдено", show_alert=True)
        return

    label = display_token_symbol(buy.token_symbol, buy.contract_address)
    await callback.message.edit_text(
        f"⚠️ Точно закрити позицію {label}?\n"
        "Це позначить її закритою в базі, БЕЗ спроби реального продажу на "
        "біржі — підходить, коли токен вже недоступний для торгівлі.",
        reply_markup=_forceclose_confirm_keyboard(buy_id),
    )
    await callback.answer()


@router.callback_query(F.data == "forceclose_cancel", is_admin)
async def cb_forceclose_cancel(callback: CallbackQuery):
    await callback.message.edit_text("Скасовано, позицію не закрито.")
    await callback.answer()


@router.callback_query(F.data.startswith("forceclose_confirm:"), is_admin)
async def cb_forceclose_confirm(callback: CallbackQuery):
    buy_id = int(callback.data.split(":", 1)[1])
    success, result_text = await _force_close_position(buy_id)
    await callback.message.edit_text(result_text)
    await callback.answer()


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

    # asyncio.to_thread — той самий синхронний Solana RPC-виклик, що й у
    # cmd_balance() вище; format_stats_report() потребує ПОТОЧНИЙ реальний
    # баланс для розрахунку PnL у відсотках (LIVE-секція).
    balance = await asyncio.to_thread(get_wallet_balance)
    # balance.is_real=False означає usdt_balance — це MOCK_WALLET_BALANCE_USD
    # (немає/невалідний SOLANA_PRIVATE_KEY), а НЕ реальний баланс гаманця —
    # використовувати його як базу для LIVE % було б хибним (той самий mock
    # вже й так є базою для DRY RUN секції, format_stats_report сам його бере).
    live_balance_usd = balance.usdt_balance if balance.is_real else None

    session = get_session()
    try:
        report = format_stats_report(session, period_key, live_wallet_usdt_balance=live_balance_usd)
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
