"""
Точка входу. Слухає Telegram-канал, парсить сигнали через Claude API,
проганяє через скринінг + ризик-менеджмент, виконує (або симулює) своп.

Запуск: python main.py
"""
import asyncio
import logging
import os
import time

from telethon import TelegramClient, events

from core.config import settings, validate_settings
from core.signal_parser import SignalParser
from core.token_screener import TokenScreener
from core.risk_manager import RiskManager
from core.okx_dex_client import OKXDexClient, SOL_NATIVE_ADDRESS
from core.storage import init_db, get_session, SignalLog, Trade
from core.wallet import MOCK_WALLET_BALANCE_USD
from core.control_bot import run_control_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

parser = SignalParser()
screener = TokenScreener()
risk = RiskManager()
dex = OKXDexClient()

HEARTBEAT_FILE = "data/heartbeat.txt"
HEARTBEAT_INTERVAL_SECONDS = 30


async def notify(client: TelegramClient, text: str):
    """Сповіщення власника бота про дію/помилку."""
    if settings.tg_notify_chat_id:
        try:
            await client.send_message(settings.tg_notify_chat_id, text)
        except Exception as e:
            logger.warning(f"Не вдалось надіслати сповіщення: {e}")
    logger.info(f"NOTIFY: {text}")


def resolve_contract_address(parsed) -> str | None:
    """
    Якщо LLM повернув лише тікер без адреси — тут МАЄ бути пошук через
    DexScreener search API або Birdeye за тікером. Навмисно НЕ автоматизовано
    повністю: торгувати по тікеру без явної адреси контракту — це прямий шлях
    нарватися на токен-двійник (скам з такою ж назвою). Тому за замовчуванням
    відхиляємо сигнали без explicit contract_address.
    """
    if parsed.contract_address:
        return parsed.contract_address
    return None


async def process_signal(client: TelegramClient, message_text: str):
    session = get_session()
    try:
        parsed = parser.parse(message_text)

        log_entry = SignalLog(
            raw_text=parsed.raw_text,
            is_signal=parsed.is_signal,
            action=parsed.action,
            token_symbol=parsed.token_symbol,
            contract_address=parsed.contract_address,
            chain=parsed.chain,
            confidence=parsed.confidence,
            reasoning=parsed.reasoning,
        )

        if not parsed.is_signal:
            log_entry.rejection_reason = "Не є торговим сигналом"
            session.add(log_entry)
            session.commit()
            return

        logger.info(
            f"Сигнал розпізнано: {parsed.action} {parsed.token_symbol} "
            f"(confidence={parsed.confidence:.2f})"
        )

        # --- Пауза через /stop в control-боті ---
        # Сигнал усе одно логується вище (бот "продовжує слухати й логувати"),
        # але далі за скринінг/quote/своп ми не йдемо, поки не /start.
        paused_check = risk.check_paused()
        if not paused_check.allowed:
            log_entry.rejection_reason = paused_check.reason
            session.add(log_entry)
            session.commit()
            return

        # --- Ризик-перевірка: впевненість ---
        conf_check = risk.check_confidence(parsed.confidence)
        if not conf_check.allowed:
            log_entry.rejection_reason = conf_check.reason
            session.add(log_entry)
            session.commit()
            await notify(client, f"⚠️ Сигнал відхилено: {conf_check.reason}\nТекст: {message_text[:200]}")
            return

        # --- Резолвинг адреси контракту ---
        contract = resolve_contract_address(parsed)
        if not contract:
            reason = "Немає явної адреси контракту в сигналі — торгівля лише по тікеру заборонена (захист від скам-двійників)"
            log_entry.rejection_reason = reason
            session.add(log_entry)
            session.commit()
            await notify(client, f"⚠️ Сигнал відхилено: {reason}")
            return

        # --- Cooldown ---
        cooldown_check = risk.check_cooldown(parsed.chain or settings.chain)
        if not cooldown_check.allowed:
            log_entry.rejection_reason = cooldown_check.reason
            session.add(log_entry)
            session.commit()
            return

        # --- Ліміт відкритих позицій (тільки для buy) ---
        if parsed.action == "buy":
            pos_check = risk.check_open_positions_limit()
            if not pos_check.allowed:
                log_entry.rejection_reason = pos_check.reason
                session.add(log_entry)
                session.commit()
                await notify(client, f"⚠️ Сигнал відхилено: {pos_check.reason}")
                return

        # --- Денний ліміт збитків ---
        loss_check = risk.check_daily_loss_limit(MOCK_WALLET_BALANCE_USD)
        if not loss_check.allowed:
            log_entry.rejection_reason = loss_check.reason
            session.add(log_entry)
            session.commit()
            await notify(client, f"🛑 БОТ ЗУПИНЕНО: {loss_check.reason}")
            return

        # --- Токен-скринінг (тільки для buy — продавати наявне можна завжди) ---
        if parsed.action == "buy":
            screening = screener.screen(contract, parsed.chain or settings.chain)
            if not screening.passed:
                reason = "Токен не пройшов скринінг: " + "; ".join(screening.reasons_failed)
                log_entry.rejection_reason = reason
                session.add(log_entry)
                session.commit()
                await notify(client, f"⚠️ Сигнал відхилено: {reason}")
                return

        # --- Розрахунок розміру позиції ---
        position_size_usd = risk.calculate_position_size(MOCK_WALLET_BALANCE_USD)

        # --- Quote ---
        from_addr = SOL_NATIVE_ADDRESS if parsed.action == "buy" else contract
        to_addr = contract if parsed.action == "buy" else SOL_NATIVE_ADDRESS
        # amount_raw має бути в найменших одиницях (lamports для SOL) — спрощено для прикладу
        amount_raw = str(int(position_size_usd * 1_000_000_000 / 150))  # припущення: SOL ~$150

        quote = dex.get_quote(from_addr, to_addr, amount_raw)
        if not quote.success:
            log_entry.rejection_reason = f"Помилка отримання quote: {quote.error}"
            session.add(log_entry)
            session.commit()
            await notify(client, f"❌ Помилка quote: {quote.error}")
            return

        impact_check = risk.check_price_impact(quote.price_impact_pct or 0)
        if not impact_check.allowed:
            log_entry.rejection_reason = impact_check.reason
            session.add(log_entry)
            session.commit()
            await notify(client, f"⚠️ Сигнал відхилено: {impact_check.reason}")
            return

        # --- Виконання (або dry-run симуляція) ---
        swap_result = dex.execute_swap(
            from_addr, to_addr, amount_raw,
            wallet_address="<буде підставлено з гаманця>",
            slippage_pct=settings.max_slippage_pct,
            chain_id="501",
        )

        trade = Trade(
            action=parsed.action,
            token_symbol=parsed.token_symbol,
            contract_address=contract,
            chain=parsed.chain or settings.chain,
            amount_usd=position_size_usd,
            tx_hash=swap_result.tx_hash,
            dry_run=swap_result.dry_run,
            status="confirmed" if swap_result.success else "failed",
        )
        session.add(trade)

        log_entry.executed = swap_result.success
        session.add(log_entry)
        session.commit()

        risk.register_trade_time(parsed.chain or settings.chain)

        prefix = "🧪 [DRY RUN] " if swap_result.dry_run else "✅ "
        await notify(
            client,
            f"{prefix}{parsed.action.upper()} {parsed.token_symbol} "
            f"на ${position_size_usd:.2f} | tx: {swap_result.tx_hash}",
        )

    finally:
        session.close()


async def heartbeat_loop():
    """
    Періодично оновлює файл-мітку часу — використовується Docker HEALTHCHECK
    (див. Dockerfile) як сигнал "процес живий і не завис у event loop".
    Це НЕ перевірка того, що Telethon-з'єднання з Telegram справне — лише
    того, що asyncio loop взагалі обробляє задачі.
    """
    os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
    while True:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def main():
    errors = validate_settings()
    if errors:
        logger.error("Помилки конфігурації:")
        for e in errors:
            logger.error(f"  - {e}")
        logger.error("Виправ .env файл перед запуском.")
        return

    init_db()

    mode = "DRY RUN (без реальних угод)" if settings.dry_run else "🔴 LIVE (реальні гроші!)"
    logger.info(f"Старт бота. Режим: {mode}. Мережа: {settings.chain}")

    client = TelegramClient(
        settings.tg_session_name, settings.tg_api_id, settings.tg_api_hash
    )

    @client.on(events.NewMessage(chats=settings.tg_channel_username))
    async def handler(event):
        text = event.message.message
        if not text:
            return
        logger.info(f"Нове повідомлення: {text[:100]}...")
        await process_signal(client, text)

    await client.start()
    logger.info(f"Слухаю канал: {settings.tg_channel_username}")

    # Listener-клієнт (Telethon, user-акаунт), control-бот (aiogram, Bot API)
    # і heartbeat крутяться в одному asyncio event loop — окремий процес для
    # control-бота ускладнив би деплой (два systemd unit / два контейнери)
    # без реальної потреби на цьому масштабі.
    await asyncio.gather(
        client.run_until_disconnected(),
        run_control_bot(),
        heartbeat_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
