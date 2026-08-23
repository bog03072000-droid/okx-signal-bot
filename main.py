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
from core.okx_dex_client import OKXDexClient, USDT_MINT_SOLANA, USDT_DECIMALS
from core.storage import init_db, get_session, SignalLog, Trade
from core.wallet import get_wallet_balance, MOCK_WALLET_BALANCE_USD
from core.control_bot import run_control_bot
from core.price_feed import fetch_price_usd
from core.formatting import display_token_symbol
from core.position_monitor import position_monitor_loop, execute_partial_sell, remaining_amount, POSITION_EPSILON

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

# Слова, що можуть означати продаж у reply-повідомленні без адреси контракту
# (канал згадує продажі саме так — див. core/signal_parser.py REPLY_SELL_SYSTEM_PROMPT).
# Це лише швидкий Python-фільтр ДО виклику LLM (щоб не палити токени Claude
# на кожен reply без ознак продажу) — остаточне рішення все одно приймає LLM.
SELL_REPLY_KEYWORDS = ["слил", "закрыл", "вышел", "зафиксил", "флипнул", "продал", "скинул"]


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
            return parsed

        # --- Підтримка мережі (поки лише Solana) ---
        # Тихий запис в лог для статистики (/status показує лічильник) — БЕЗ
        # сповіщення власнику: канал регулярно кидає EVM-адреси поряд з
        # Solana, і сповіщення про кожну засмічувало б чат. EVM-підтримка
        # запланована на майбутнє, зараз бот свідомо не намагається торгувати
        # тим, що не вміє виконати.
        if parsed.chain and parsed.chain != "solana":
            log_entry.rejection_reason = (
                f"Мережа {parsed.chain} поки не підтримується ботом (тільки Solana). "
                "EVM-підтримка запланована на майбутнє."
            )
            session.add(log_entry)
            session.commit()
            return parsed

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
            return parsed

        # --- Ризик-перевірка: впевненість ---
        conf_check = risk.check_confidence(parsed.confidence)
        if not conf_check.allowed:
            log_entry.rejection_reason = conf_check.reason
            session.add(log_entry)
            session.commit()
            await notify(client, f"⚠️ Сигнал відхилено: {conf_check.reason}\nТекст: {message_text[:200]}")
            return parsed

        # --- Резолвинг адреси контракту ---
        contract = resolve_contract_address(parsed)
        if not contract:
            reason = "Немає явної адреси контракту в сигналі — торгівля лише по тікеру заборонена (захист від скам-двійників)"
            log_entry.rejection_reason = reason
            session.add(log_entry)
            session.commit()
            await notify(client, f"⚠️ Сигнал відхилено: {reason}")
            return parsed

        # --- Cooldown ---
        cooldown_check = risk.check_cooldown(parsed.chain or settings.chain)
        if not cooldown_check.allowed:
            log_entry.rejection_reason = cooldown_check.reason
            session.add(log_entry)
            session.commit()
            return parsed

        # --- Ліміт відкритих позицій (тільки для buy) ---
        if parsed.action == "buy":
            pos_check = risk.check_open_positions_limit()
            if not pos_check.allowed:
                log_entry.rejection_reason = pos_check.reason
                session.add(log_entry)
                session.commit()
                await notify(client, f"⚠️ Сигнал відхилено: {pos_check.reason}")
                return parsed

        # --- Баланс гаманця (USDT — торговий капітал, SOL — резерв на газ) ---
        wallet_balance = get_wallet_balance()

        if not settings.dry_run and wallet_balance.usdt_balance is None:
            # У LIVE-режимі свідомо НЕ підставляємо MOCK_WALLET_BALANCE_USD, якщо
            # реальний баланс USDT не вдалось отримати (RPC впав/rate limit) —
            # краще відхилити сигнал, ніж порахувати розмір реальної позиції
            # наосліп по вигаданому числу.
            reason = "Не вдалось отримати реальний баланс USDT з RPC — угоду відхилено (безпечніше не торгувати, ніж рахувати позицію наосліп)"
            log_entry.rejection_reason = reason
            session.add(log_entry)
            session.commit()
            await notify(client, f"🛑 {reason}")
            return parsed

        if wallet_balance.low_gas_warning:
            await notify(client, f"⛽ {wallet_balance.note}")

        effective_balance_usd = (
            wallet_balance.usdt_balance if wallet_balance.usdt_balance is not None else MOCK_WALLET_BALANCE_USD
        )

        # --- Денний ліміт збитків ---
        loss_check = risk.check_daily_loss_limit(effective_balance_usd)
        if not loss_check.allowed:
            log_entry.rejection_reason = loss_check.reason
            session.add(log_entry)
            session.commit()
            await notify(client, f"🛑 БОТ ЗУПИНЕНО: {loss_check.reason}")
            return parsed

        # --- Токен-скринінг (тільки для buy — продавати наявне можна завжди) ---
        if parsed.action == "buy":
            screening = screener.screen(contract, parsed.chain or settings.chain)
            if not screening.passed:
                reason = "Токен не пройшов скринінг: " + "; ".join(screening.reasons_failed)
                log_entry.rejection_reason = reason
                session.add(log_entry)
                session.commit()
                await notify(client, f"⚠️ Сигнал відхилено: {reason}")
                return parsed

        if parsed.action == "buy":
            # --- Розрахунок розміру позиції ---
            position_size_usd = risk.calculate_position_size(effective_balance_usd)

            # --- Quote ---
            # USDT — базова торгова валюта (замість native SOL, який лишається лише
            # на газ — див. core/wallet.py). USDT — стейблкоїн, курс до USD ~1:1 за
            # визначенням, тому USD-сума ≈ USDT-сума напряму, без курсу конвертації.
            #
            # СКЕПТИЧНИЙ КОМЕНТАР: "~1:1" — це припущення, не гарантія. USDT теоретично
            # може де-пегнутись (тимчасово чи стійко відхилитись від $1 — таке бувало
            # з іншими стейблкоїнами при стресі на ринку). Для точного розрахунку
            # реальної вартості свопу варто орієнтуватись на fromTokenAmount/
            # toTokenAmount із самого quote (нижче), а не на цю грубу оцінку "1 USDT = $1" —
            # тут це прийнятно лише для розрахунку amount_raw ПЕРЕД запитом quote.
            from_addr = USDT_MINT_SOLANA
            to_addr = contract
            # amount_raw — у найменших одиницях USDT (6 decimals, на відміну від 9 у SOL/lamports)
            amount_raw = str(int(position_size_usd * (10 ** USDT_DECIMALS)))

            if int(amount_raw) <= 0:
                # Найчастіша причина — гаманець ще не поповнений USDT (баланс 0,
                # тож і calculate_position_size() дає 0). Без цієї перевірки бот
                # робив би реальний запит quote до OKX з amount=0 щоразу на кожен
                # сигнал — зайве навантаження на API і незрозуміла помилка від
                # OKX замість чіткої причини тут.
                reason = (
                    "Розрахований розмір угоди — 0 USDT (ймовірно, баланс USDT порожній). "
                    "Сигнал відхилено без запиту quote."
                )
                log_entry.rejection_reason = reason
                session.add(log_entry)
                session.commit()
                return parsed

            quote = dex.get_quote(from_addr, to_addr, amount_raw)
            if not quote.success:
                log_entry.rejection_reason = f"Помилка отримання quote: {quote.error}"
                session.add(log_entry)
                session.commit()
                await notify(client, f"❌ Помилка quote: {quote.error}")
                return parsed

            impact_check = risk.check_price_impact(quote.price_impact_pct or 0)
            if not impact_check.allowed:
                log_entry.rejection_reason = impact_check.reason
                session.add(log_entry)
                session.commit()
                await notify(client, f"⚠️ Сигнал відхилено: {impact_check.reason}")
                return parsed

            # --- Виконання (або dry-run симуляція) ---
            swap_result = dex.execute_swap(
                from_addr, to_addr, amount_raw,
                wallet_address="<буде підставлено з гаманця>",
                slippage_pct=settings.max_slippage_pct,
                chain_id="501",
            )

            trade = Trade(
                action="buy",
                token_symbol=parsed.token_symbol,
                contract_address=contract,
                chain=parsed.chain or settings.chain,
                amount_usd=position_size_usd,
                tx_hash=swap_result.tx_hash,
                dry_run=swap_result.dry_run,
                status="confirmed" if swap_result.success else "failed",
            )

            if swap_result.success:
                # token_amount — RAW (найменші одиниці) отриманого токена, напряму
                # з quote.to_amount (а не через ручний перерахунок decimals) — це
                # саме ті одиниці, які знадобляться напряму для amount_raw
                # майбутніх часткових sell у core/position_monitor.py (ladder TP/SL).
                try:
                    trade.token_amount = float(quote.to_amount) if quote.to_amount else None
                except (TypeError, ValueError):
                    trade.token_amount = None

                # entry_price — ринкова ціна ОДРАЗУ після виконання (з DexScreener,
                # а не з quote — quote дає курс СВОПУ, а не ту саму ціну, яку буде
                # звіряти монітор позицій; звіряємо яблука з яблуками).
                # Якщо lookup не вдався — trade.entry_price лишається None, і
                # core/position_monitor.py._get_open_positions() свідомо пропускає
                # такі позиції — жодного автоматичного SL/TP на них не буде,
                # лишається тільки ручний /positions.
                trade.entry_price = fetch_price_usd(contract, parsed.chain or settings.chain)
                if trade.entry_price is None:
                    logger.warning(
                        f"Не вдалось отримати entry_price для {parsed.token_symbol} — "
                        "ladder TP/SL моніторинг НЕ підхопить цю позицію."
                    )

            session.add(trade)

            log_entry.executed = swap_result.success
            session.add(log_entry)
            session.commit()

            risk.register_trade_time(parsed.chain or settings.chain)

            prefix = "🧪 [DRY RUN] " if swap_result.dry_run else "✅ "
            await notify(
                client,
                f"{prefix}BUY {display_token_symbol(parsed.token_symbol, contract)} на ${position_size_usd:.2f} | tx: {swap_result.tx_hash}",
            )

            return parsed

        else:
            # --- SELL по прямому сигналу з каналу (контракт вказано явно в тексті) ---
            # На відміну від reply-sell (process_reply_sell нижче) і ladder TP/SL
            # (core/position_monitor.py), тут контракт відомий одразу з самого
            # сигналу — окремого LLM-виклику для пошуку контракту не треба.
            #
            # Продає 100% залишку ВІДСТЕЖЕНОЇ buy-позиції (через parent_trade_id,
            # execute_partial_sell) — НЕ довільний % від балансу USDT, як було
            # раніше: сигнал "продай X" стосується ТОКЕНА, яким бот реально
            # володіє, а не суми в USDT (амаунт у USDT для продажу ТОКЕНА не мав
            # сенсу — це й був корінь того, що PnL взагалі не можна було порахувати
            # для цього шляху, бо не було зв'язку з конкретною buy-позицією).
            # Якщо колись amount_hint навчаться парсити як частку ("продай
            # половину") — сюди можна підставити fraction < 1.0, наразі завжди
            # повний вихід із позиції.
            buy = session.query(Trade).filter(
                Trade.action == "buy",
                Trade.status == "confirmed",
                Trade.contract_address == contract,
            ).order_by(Trade.created_at.desc()).first()

            if not buy or remaining_amount(session, buy) <= POSITION_EPSILON:
                reason = "Sell-сигнал по контракту без відстежуваної відкритої позиції в БД — нічого продавати"
                log_entry.rejection_reason = reason
                session.add(log_entry)
                session.commit()
                return parsed

            sell_amount = remaining_amount(session, buy)
            current_price = fetch_price_usd(contract, parsed.chain or settings.chain)

            # execute_partial_sell сама пише Trade (з pnl_usd) і сповіщає власника
            # через control-бота — окремий notify() тут не потрібен.
            success = await execute_partial_sell(session, buy, "signal_sell", sell_amount, current_price)

            log_entry.executed = success
            session.add(log_entry)
            session.commit()

            if success:
                risk.register_trade_time(parsed.chain or settings.chain)

            return parsed

    finally:
        session.close()


async def process_reply_sell(client: TelegramClient, event):
    """
    Best-effort обробка sell-згадок у reply без адреси контракту (напр.
    "слил половину" у відповідь на оригінальний кол). Викликається з
    handler() ТІЛЬКИ коли: це reply, звичайний parse() оригінального
    reply-тексту вже сказав is_signal=false, і текст містить одне з
    SELL_REPLY_KEYWORDS — це додатковий сигнал на закриття В ПАРІ З ladder
    TP/SL (core/position_monitor.py), а НЕ заміна йому: якщо ladder вже
    закрив позицію повністю, цей heuristic просто ігнорує "нема чого продавати".
    """
    reply_text = event.message.message
    if not reply_text:
        return

    try:
        original_msg = await client.get_messages(event.chat_id, ids=event.message.reply_to_msg_id)
    except Exception as e:
        logger.warning(f"Reply-sell: не вдалось отримати оригінальне повідомлення: {e}")
        return
    if not original_msg or not original_msg.message:
        return

    original_parsed = parser.parse(original_msg.message)
    if not original_parsed.contract_address:
        logger.info("Reply-sell: оригінал не містить адреси контракту — пропускаємо")
        return

    reply_result = parser.parse_reply_sell(reply_text, original_msg.message)
    if not reply_result["is_sell_signal"] or reply_result["confidence"] < settings.min_signal_confidence:
        logger.info(
            f"Reply-sell: LLM не підтвердив sell для {original_parsed.token_symbol} "
            f"(confidence={reply_result['confidence']:.2f}): {reply_result['reasoning']}"
        )
        return

    fraction = reply_result.get("sell_fraction")
    if fraction is None:
        fraction = 0.5  # евристичний дефолт, коли з тексту неясно яка саме частка
    fraction = max(0.0, min(1.0, float(fraction)))

    session = get_session()
    try:
        buy = session.query(Trade).filter(
            Trade.action == "buy",
            Trade.status == "confirmed",
            Trade.contract_address == original_parsed.contract_address,
        ).order_by(Trade.created_at.desc()).first()

        if not buy or remaining_amount(session, buy) <= POSITION_EPSILON:
            logger.info(
                f"Reply-sell: позиція {original_parsed.token_symbol} ({original_parsed.contract_address}) "
                "вже закрита або не знайдена в БД — ігноруємо (ймовірно, ladder TP/SL вже закрив її)."
            )
            return

        remaining = remaining_amount(session, buy)
        sell_amount = remaining * fraction
        current_price = fetch_price_usd(original_parsed.contract_address, original_parsed.chain or settings.chain)
        close_reason = f"reply_sell_heuristic_{int(round(fraction * 100))}pct"

        logger.info(
            f"Reply-sell: продаємо {fraction:.0%} залишку {original_parsed.token_symbol} "
            f"(евристика з тексту reply, LLM confidence={reply_result['confidence']:.2f}): "
            f"{reply_result['reasoning']}"
        )
        await execute_partial_sell(session, buy, close_reason, sell_amount, current_price)
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
        parsed = await process_signal(client, text)

        # Reply-sell евристика: тільки якщо це reply, звичайний парсинг сказав
        # "не сигнал" (типово — бо нема адреси контракту в самому reply), і
        # текст схожий на sell-згадку. Швидкий keyword-фільтр ДО виклику LLM,
        # щоб не витрачати запити Claude на кожен reply без жодних ознак продажу.
        if (
            event.message.reply_to_msg_id
            and parsed is not None
            and not parsed.is_signal
            and any(kw in text.lower() for kw in SELL_REPLY_KEYWORDS)
        ):
            await process_reply_sell(client, event)

    await client.start()
    logger.info(f"Слухаю канал: {settings.tg_channel_username}")

    # Listener-клієнт (Telethon, user-акаунт), control-бот (aiogram, Bot API),
    # ladder TP/SL монітор позицій і heartbeat крутяться в одному asyncio
    # event loop — окремий процес для кожного ускладнив би деплой (кілька
    # systemd unit / кілька контейнерів) без реальної потреби на цьому масштабі.
    await asyncio.gather(
        client.run_until_disconnected(),
        run_control_bot(),
        position_monitor_loop(),
        heartbeat_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
