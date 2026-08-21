"""
Telegram control-бот керування (Bot API, aiogram) — окремий бот-акаунт
(створюється через @BotFather), працює паралельно з listener-клієнтом
на Telethon у main.py в тому самому asyncio event loop.

Доступ жорстко обмежений одним TG_OWNER_USER_ID з .env: усі команди від
будь-кого іншого мовчки ігноруються (є лише warning у лог, без відповіді
в чат) — щоб стороння людина, яка випадково знайде бота, навіть не бачила,
що він на щось реагує.

СКЕПТИЧНИЙ КОМЕНТАР: єдина авторизація тут — Telegram user_id. Якщо власник
колись втратить контроль над своїм Telegram-акаунтом (злом/SIM-swap), той,
хто його перехопить, отримає й контроль над цим ботом (/stop, /setlimit).
Для реальних грошей варто розглянути додатковий фактор (напр. підтвердження
критичних команд кодом з .env), але це свідомо не реалізовано зараз.
"""
import datetime as dt
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func

from core import runtime_state
from core.config import settings, LIMIT_FIELDS, get_limit, is_limit_overridden
from core.storage import get_session, SignalLog, Trade
from core.wallet import get_wallet_balance

logger = logging.getLogger(__name__)

router = Router(name="control_bot")


class IsOwner(BaseFilter):
    """Пропускає повідомлення лише від TG_OWNER_USER_ID, решту мовчки логує й відхиляє."""

    async def __call__(self, message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else None
        if not settings.tg_owner_user_id or user_id != settings.tg_owner_user_id:
            logger.warning(
                f"Control-бот: команда від user_id={user_id} проігнорована (не власник)"
            )
            return False
        return True


router.message.filter(IsOwner())


def _today_start() -> dt.datetime:
    return dt.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


@router.message(Command("status"))
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

        await message.answer(
            "📊 <b>Статус бота</b>\n"
            f"Режим: {mode}\n"
            f"Торгівля: {pause_state}\n"
            f"Сигналів сьогодні: {signals_today}\n"
            f"Виконано угод: {executed_today}\n"
            f"Відхилено: {rejected_today}\n"
            f"Відхилено через непідтримувану мережу: {unsupported_chain_today}",
            parse_mode="HTML",
        )
    finally:
        session.close()


@router.message(Command("balance"))
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
    buys = session.query(func.sum(Trade.amount_usd)).filter(
        Trade.action == "buy", Trade.status == "confirmed"
    ).scalar() or 0.0
    sells = session.query(func.sum(Trade.amount_usd)).filter(
        Trade.action == "sell", Trade.status == "confirmed"
    ).scalar() or 0.0
    return max(buys - sells, 0.0)


@router.message(Command("positions"))
async def cmd_positions(message: Message):
    session = get_session()
    try:
        # Групування "відкритих" позицій по токену: сума buy мінус сума sell в USD.
        # Це НЕ реальна кількість токенів і НЕ поточний PnL — просто скільки USD
        # ще "в грі" по токену станом на ціни купівлі. Реальний PnL вимагає окремого
        # моніторингу поточної ціни токена (не реалізовано, див. README).
        rows = session.query(
            Trade.token_symbol,
            Trade.action,
            func.sum(Trade.amount_usd),
        ).filter(Trade.status == "confirmed").group_by(Trade.token_symbol, Trade.action).all()

        net_by_token: dict[str, float] = {}
        for token_symbol, action, total in rows:
            net_by_token.setdefault(token_symbol, 0.0)
            net_by_token[token_symbol] += total if action == "buy" else -total

        open_positions = {t: v for t, v in net_by_token.items() if v > 0.01}

        if not open_positions:
            await message.answer("📭 Немає відкритих позицій.")
            return

        lines = ["📈 <b>Відкриті позиції</b> (PnL н/д — без моніторингу ціни в реальному часі)"]
        for token, usd in sorted(open_positions.items(), key=lambda x: -x[1]):
            lines.append(f"• {token}: ≈ ${usd:,.2f}")
        await message.answer("\n".join(lines), parse_mode="HTML")
    finally:
        session.close()


@router.message(Command("history"))
async def cmd_history(message: Message, command: CommandObject):
    limit = 10
    if command.args:
        try:
            limit = max(1, min(50, int(command.args.strip())))
        except ValueError:
            await message.answer("Використання: /history [число від 1 до 50]")
            return

    session = get_session()
    try:
        trades = session.query(Trade).order_by(Trade.created_at.desc()).limit(limit).all()
        if not trades:
            await message.answer("Історія угод порожня.")
            return

        lines = [f"🕓 <b>Останні {len(trades)} угод</b>"]
        for t in trades:
            prefix = "🧪" if t.dry_run else "💵"
            ts = t.created_at.strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"{prefix} {ts} | {t.action.upper()} {t.token_symbol} "
                f"${t.amount_usd:.2f} | {t.status}"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")
    finally:
        session.close()


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    runtime_state.set_paused(True)
    await message.answer(
        "⏸️ Торгівлю призупинено. Бот продовжує слухати канал і логувати сигнали, "
        "але НЕ виконуватиме жодних свопів, поки не викличеш /start."
    )


@router.message(Command("start"))
async def cmd_start(message: Message):
    runtime_state.set_paused(False)
    await message.answer("▶️ Торгівлю відновлено.")


@router.message(Command("limits"))
async def cmd_limits(message: Message):
    lines = ["⚙️ <b>Поточні ризик-ліміти</b>"]
    for env_name, (_, _, unit) in LIMIT_FIELDS.items():
        value = get_limit(env_name)
        marker = " (перевизначено /setlimit)" if is_limit_overridden(env_name) else ""
        lines.append(f"• {env_name} = {value} {unit}{marker}")
    lines.append("\nЗмінити: /setlimit НАЗВА значення (напр. /setlimit MAX_OPEN_POSITIONS 5)")
    lines.append("Скинути до .env: /setlimit НАЗВА default")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("setlimit"))
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


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📋 <b>Команди</b>\n"
        "/status — статус бота і статистика за сьогодні\n"
        "/balance — баланс гаманця\n"
        "/positions — відкриті позиції\n"
        "/history [N] — останні N угод (default 10)\n"
        "/stop — призупинити торгівлю\n"
        "/start — відновити торгівлю\n"
        "/limits — поточні ризик-ліміти\n"
        "/setlimit НАЗВА значення — змінити ліміт на льоту",
        parse_mode="HTML",
    )


_bot: "Bot | None" = None


async def notify_owner(text: str):
    """
    Сповіщення власнику ЧЕРЕЗ CONTROL-БОТА (не через Telethon-listener і не
    через TG_NOTIFY_CHAT_ID) — окремий канал для подій, що стаються поза
    командним потоком, напр. автоматичне спрацювання ladder TP/SL з
    core/position_monitor.py. Свідомо відокремлено від сповіщень про сигнали
    з каналу (main.py notify()), щоб було видно, звідки прийшла подія.
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
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Control-бот запущено (aiogram, Bot API), доступ обмежено TG_OWNER_USER_ID")
    await dp.start_polling(_bot)
