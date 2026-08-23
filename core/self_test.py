"""
Тестовий прогін для кнопки "🧪 Тест" у control-боті: повний торговий пайплайн
(парсинг → скринінг → ризик-перевірки → quote → "своп") на реальному
прикладному повідомленні з каналу, і окремо — ladder TP/SL на симульованій
позиції.

БЕЗПЕКА: усе тут ГАРАНТОВАНО симуляція, незалежно від DRY_RUN у .env.
Єдине місце, де взагалі МІГ БИ статись реальний своп — виклик
OKXDexClient.execute_swap() — тут завжди отримує force_dry_run=True як
ЛІТЕРАЛ (не змінну, не settings.dry_run) прямо в місці виклику. Дивись
докладний коментар у core/okx_dex_client.py, чому це окремий прапорець,
а не покладання на settings.dry_run.

Навмисно НЕ імпортує main.py: main.py імпортує core.control_bot (щоб
запустити control-бота), а цей модуль викликається З control_bot.py —
якби він імпортував main.py, вийшов би цикл main -> control_bot -> self_test
-> main. Тому тут створюються ОКРЕМІ інстанси SignalParser/TokenScreener/
RiskManager/OKXDexClient — самі КЛАСИ і методи ті самі, що використовує
main.py в реальному пайплайні process_signal() (RiskManager._last_trade_time —
module-level стан у core/risk_manager.py, спільний для БУДЬ-ЯКОЇ кількості
інстансів RiskManager) — просто інша Python-змінна на той самий код, а не
copy-paste переписана логіка.

Ladder-логіка (_check_position, remaining_amount, _triggered_levels)
імпортується напряму з core/position_monitor.py — той самий, не copy-paste
код, що працює в реальному position_monitor_loop().
"""
import asyncio
import logging

from core.config import settings, get_limit
from core.signal_parser import SignalParser
from core.token_screener import TokenScreener
from core.risk_manager import RiskManager
from core.okx_dex_client import OKXDexClient, USDT_MINT_SOLANA, USDT_DECIMALS
from core.wallet import get_wallet_balance, MOCK_WALLET_BALANCE_USD
from core.storage import get_session, Trade, TEST_TOKEN_SYMBOL
from core.position_monitor import _check_position, _triggered_levels, remaining_amount
from core.formatting import format_price_usd

logger = logging.getLogger(__name__)

# Окремі інстанси — див. docstring вище щодо циклічного імпорту.
_parser = SignalParser()
_screener = TokenScreener()
_risk = RiskManager()
_dex = OKXDexClient()

# Реальний приклад повідомлення з каналу — той самий текст, що вже є
# few-shot прикладом 1 у core/signal_parser.py SYSTEM_PROMPT.
TEST_SIGNAL_TEXT = "16k, дев очень жир JACCJHVy2QC96VNJK1iMrqYwMQPBbHNna2oEnxEPpump"

# Контракт для симульованих ladder-позицій — СВІДОМО справжня, завжди
# ліквідна адреса, а НЕ вигадана. get_quote() у _check_position()/
# execute_partial_sell() (core/position_monitor.py) НІКОЛИ не гейтиться
# dry_run/force_dry_run — це завжди РЕАЛЬНИЙ (але суто інформаційний,
# нічого не рухає) запит до OKX за котируванням. Вигадана неіснуюча адреса
# змусила б OKX відхилити quote в реальному деплої з реальними ключами —
# і ladder-тест хибно показував би "рівень не спрацював" через відсутність
# котирування, а не через реальний баг.
#
# ВАЖЛИВО, перевірено на реальному деплої (не з пам'яті): тут НЕ можна
# використовувати core.okx_dex_client.SOL_NATIVE_ADDRESS
# ("11111111111111111111111111111111") — це насправді Solana System
# Program ID, не SPL-токен. Деякі агрегатори (Jupiter) приймають цю адресу
# як умовне позначення "нативний SOL" і самі його (роз)обгортають, але OKX
# DEX Aggregator API так НЕ робить — quote з цією адресою відхиляється.
# Офіційний приклад OKX (github.com/okx/dex-api-library) використовує саме
# WRAPPED SOL mint нижче. Перевірено ще й напряму по RPC (getAccountInfo):
# decimals=9, owner=TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA (стандартна
# SPL Token Program) — це справжній, котируваний токен, на відміну від
# SOL_NATIVE_ADDRESS. Плутанини з "торгівлею SOL" використання тут не
# створює: сам своп все одно НІКОЛИ не виконується (force_dry_run=True),
# а Trade-рядки позначені token_symbol=TEST_LADDER_TOKEN_SYMBOL нижче —
# саме за цим полем відбувається і показ, і прибирання після тесту, а не
# за адресою контракту.
TEST_LADDER_CONTRACT = "So11111111111111111111111111111111111111112"  # Wrapped SOL mint
# Аліас на єдине джерело істини (core/storage.py) — див. коментар там щодо
# того, чому саме storage.py, а не цей файл, визначає це значення.
TEST_LADDER_TOKEN_SYMBOL = TEST_TOKEN_SYMBOL

# Пауза між послідовними quote-викликами всередині run_ladder_test().
# ПЕРЕВІРЕНО на реальному деплої (docker logs): без паузи 6 quote-запитів
# підряд (діагностика + 2×SL + 3×TP) впираються в rate limit OKX —
# "429 Too Many Requests" на 4-му+ запиті за частку секунди. Реальний
# position_monitor_loop() робить лише 1 такий запит на позицію раз/2с,
# тому цей rate limit — це артефакт саме тесту (він б'є по API набагато
# швидше за прод), а НЕ баг ladder-логіки і не проблема реальної торгівлі.
QUOTE_CALL_DELAY_SECONDS = 1.0


def _ok(msg: str) -> str:
    return f"✅ {msg}"


def _fail(msg: str) -> str:
    return f"❌ {msg}"


async def run_buy_signal_test() -> list[str]:
    """
    Прожене TEST_SIGNAL_TEXT через ТОЙ САМИЙ послідовний ланцюжок перевірок,
    що й main.py:process_signal() (крок у крок та сама послідовність і ті самі
    методи _risk/_screener/_dex/get_wallet_balance) — окремий шлях, а не
    виклик самого process_signal(), щоб не писати реальні SignalLog/Trade і не
    слати реальні Telegram-сповіщення через notify() під час тесту.

    Якщо якийсь крок відхилив би угоду в реальному пайплайні — звіт
    зупиняється саме на ньому (той самий early-return, що й у process_signal()).
    """
    lines = [
        "🧪 <b>ТЕСТ (симуляція, реальні угоди НЕ виконуються)</b>",
        f"Сигнал: <i>{TEST_SIGNAL_TEXT}</i>",
        "",
    ]
    n = 0

    def step(label: str) -> str:
        nonlocal n
        n += 1
        return f"{n}️⃣ {label}"

    parsed = _parser.parse(TEST_SIGNAL_TEXT)
    if not parsed.is_signal:
        lines.append(step(f"Парсинг сигналу: {_fail('is_signal=false')} — {parsed.reasoning}"))
        return lines
    lines.append(step(
        f"Парсинг сигналу: {_ok(f'is_signal=true, action={parsed.action}, confidence={parsed.confidence:.2f}')}"
    ))

    if parsed.chain and parsed.chain != "solana":
        lines.append(step(f"Мережа: {_fail(f'{parsed.chain} поки не підтримується (тільки Solana)')}"))
        return lines
    lines.append(step(f"Мережа: {_ok(parsed.chain or 'solana')}"))

    paused_check = _risk.check_paused()
    if not paused_check.allowed:
        lines.append(step(f"Пауза: {_fail(paused_check.reason)}"))
        return lines
    lines.append(step(f"Пауза: {_ok('не на паузі')}"))

    conf_check = _risk.check_confidence(parsed.confidence)
    if not conf_check.allowed:
        lines.append(step(f"Впевненість сигналу: {_fail(conf_check.reason)}"))
        return lines
    lines.append(step(f"Впевненість сигналу: {_ok('пройдено')}"))

    contract = parsed.contract_address
    if not contract:
        lines.append(step(f"Резолвинг контракту: {_fail('немає адреси в сигналі')}"))
        return lines
    lines.append(step(f"Резолвинг контракту: {_ok(f'адреса знайдена ({contract[:10]}...)')}"))

    cooldown_check = _risk.check_cooldown(parsed.chain or settings.chain)
    if not cooldown_check.allowed:
        lines.append(step(f"Cooldown: {_fail(cooldown_check.reason)}"))
        return lines
    lines.append(step(f"Cooldown: {_ok('пройдено')}"))

    if parsed.action == "buy":
        pos_check = _risk.check_open_positions_limit()
        if not pos_check.allowed:
            lines.append(step(f"Ліміт відкритих позицій: {_fail(pos_check.reason)}"))
            return lines
        lines.append(step(f"Ліміт відкритих позицій: {_ok('пройдено')}"))

    wallet_balance = get_wallet_balance()
    if not settings.dry_run and wallet_balance.usdt_balance is None:
        lines.append(step(f"Баланс гаманця: {_fail('не вдалось отримати реальний баланс USDT з RPC')}"))
        return lines
    effective_balance_usd = (
        wallet_balance.usdt_balance if wallet_balance.usdt_balance is not None else MOCK_WALLET_BALANCE_USD
    )
    mock_note = " (mock, гаманець не підключено або баланс недоступний)" if wallet_balance.usdt_balance is None else ""
    lines.append(step(f"Баланс гаманця: {_ok(f'${effective_balance_usd:,.2f} USDT{mock_note}')}"))

    loss_check = _risk.check_daily_loss_limit(effective_balance_usd)
    if not loss_check.allowed:
        lines.append(step(f"Денний ліміт збитків: {_fail(loss_check.reason)}"))
        return lines
    lines.append(step(f"Денний ліміт збитків: {_ok('пройдено')}"))

    if parsed.action == "buy":
        screening = _screener.screen(contract, parsed.chain or settings.chain)
        if not screening.passed:
            lines.append(step(f"Скринінг токена: {_fail('; '.join(screening.reasons_failed))}"))
            return lines
        liq = f"${screening.liquidity_usd:,.0f}" if screening.liquidity_usd is not None else "н/д"
        age = f"{screening.age_hours:.1f}год" if screening.age_hours is not None else "н/д"
        lines.append(step(f"Скринінг токена: {_ok(f'ліквідність {liq}, вік {age}')}"))

    position_size_usd = _risk.calculate_position_size(effective_balance_usd)
    lines.append(step(
        f"Розмір позиції: ${position_size_usd:.2f} "
        f"({get_limit('MAX_POSITION_PCT')}% від балансу ${effective_balance_usd:,.2f})"
    ))

    from_addr = USDT_MINT_SOLANA if parsed.action == "buy" else contract
    to_addr = contract if parsed.action == "buy" else USDT_MINT_SOLANA
    amount_raw = str(int(position_size_usd * (10 ** USDT_DECIMALS)))

    if int(amount_raw) <= 0:
        lines.append(step(f"Quote: {_fail('розрахований розмір угоди — 0 USDT (баланс порожній)')}"))
        return lines

    quote = _dex.get_quote(from_addr, to_addr, amount_raw)
    if not quote.success:
        lines.append(step(f"Quote від OKX: {_fail(quote.error)}"))
        return lines
    impact_str = f"{quote.price_impact_pct:.2f}%" if quote.price_impact_pct is not None else "н/д"
    lines.append(step(f"Quote від OKX: {_ok(f'отримано, price impact {impact_str}')}"))

    impact_check = _risk.check_price_impact(quote.price_impact_pct or 0)
    if not impact_check.allowed:
        lines.append(step(f"Price impact: {_fail(impact_check.reason)}"))
        return lines
    lines.append(step(f"Price impact: {_ok('пройдено')}"))

    # --- "Своп" — ГАРАНТОВАНО форсована симуляція ---
    # force_dry_run=True тут — буквальний ЛІТЕРАЛ, не змінна, не settings.dry_run.
    # Навіть якщо settings.dry_run десь помилково стане False під час цього
    # виклику, execute_swap() все одно НЕ відправить реальну транзакцію,
    # бо перевіряє "settings.dry_run OR force_dry_run" — а тут force_dry_run
    # завжди True, незалежно від будь-якого зовнішнього стану.
    swap_result = _dex.execute_swap(
        from_addr, to_addr, amount_raw,
        wallet_address="<тест — гаманець не використовується>",
        slippage_pct=settings.max_slippage_pct,
        chain_id="501",
        force_dry_run=True,
    )
    lines.append(step(f"[СИМУЛЯЦІЯ] Своп: {_ok(f'виконано умовно, tx={swap_result.tx_hash}')}"))
    lines.append("")
    lines.append(
        f"<i>Якби це прийшло з реального каналу — бот би {'КУПИВ' if parsed.action == 'buy' else 'ПРОДАВ'} "
        f"на ${position_size_usd:.2f}. Тест нічого не записав у /history.</i>"
    )
    return lines


async def run_ladder_test() -> list[str]:
    """
    Прожене ladder TP/SL (core/position_monitor.py._check_position, той самий
    код, що й реальний position_monitor_loop()) на двох тимчасових позиціях:
    одна для сценарію stop-loss (-10% → -20%), друга для take-profit
    (+30% → +60% → +100%, "скинута" — свіжа позиція, окремо від SL-сценарію).

    Тимчасові Trade-рядки позначені token_symbol=TEST_LADDER_TOKEN_SYMBOL
    ("TEST_TOKEN") — за цим полем відбувається і прибирання НАПРИКІНЦІ
    (разом з усіма дочірніми sell, які встигли створитись), щоб /history і
    /positions ніколи не показували тестове сміття, навіть тимчасово.
    Контракт — TEST_LADDER_CONTRACT (справжня адреса, див. коментар вище).
    """
    lines = ["🧪 <b>ТЕСТ Ladder TP/SL (симуляція, без реальних угод)</b>", ""]
    entry_price = 0.001

    # Діагностична перевірка ПЕРЕД усіма сценаріями: get_quote() для
    # TEST_LADDER_CONTRACT — той самий виклик, що піде на кожен рівень
    # нижче. Якщо він не проходить (напр. проблема з OKX-ключами чи
    # мережею) — усі 5 рівнів однаково "не спрацюють" з тієї самої причини,
    # і без цієї перевірки звіт показував би незрозуміле "баг у
    # ladder-логіці" 5 разів замість справжньої причини один раз.
    diagnostic = _dex.get_quote(TEST_LADDER_CONTRACT, USDT_MINT_SOLANA, "1000000")
    if not diagnostic.success:
        lines.append(_fail(f"Діагностика quote() для тестового контракту не пройшла: {diagnostic.error}"))
        lines.append(
            "Це проблема отримання котирування від OKX (ключі/мережа/самі API), "
            "а НЕ баг у ladder-логіці — рівні нижче не тестуються, бо всі "
            "однаково впали б на цьому ж кроці."
        )
        return lines

    session = get_session()
    try:
        buy_sl = Trade(
            action="buy", token_symbol=TEST_LADDER_TOKEN_SYMBOL,
            contract_address=TEST_LADDER_CONTRACT,
            # chain="solana_test", НЕ "solana" — ПЕРЕВІРЕНО на реальному
            # деплої (docker logs): справжній фоновий position_monitor_loop()
            # фільтрує позиції рівно по Trade.chain=="solana" і бере ВЖЕ
            # ігнорує chain, тому раніше він бачив ці тимчасові тестові
            # рядки як звичайну відкриту позицію (справжня адреса контракту +
            # entry_price/token_amount задані) і сам, незалежно від сценарію
            # тесту, продавав їх по РЕАЛЬНІЙ ринковій ціні Wrapped SOL —
            # спостерігали в логах "+9421900.0% від входу" (реальна ціна ~$94
            # проти фейкового entry_price=0.001 тесту), що гонково спотворювало
            # результати послідовних кроків run_ladder_test(). Прямі виклики
            # _check_position() нижче не залежать від Trade.chain і тому не
            # зачіпаються цією зміною.
            chain="solana_test", amount_usd=10.0, tx_hash="TEST_SIMULATION_NO_TX",
            dry_run=True, status="confirmed",
            entry_price=entry_price, token_amount=1_000_000.0,
            triggered_levels="[]",
        )
        session.add(buy_sl)
        session.commit()

        # Пауза перед КОЖНИМ наступним quote-викликом нижче — див. коментар
        # QUOTE_CALL_DELAY_SECONDS вище (без неї — 429 від OKX).
        await asyncio.sleep(QUOTE_CALL_DELAY_SECONDS)
        price_sl10 = entry_price * 0.90
        lines.append(f"Сценарій SL: вхід {format_price_usd(entry_price)} → ціна {format_price_usd(price_sl10)} (-10%)")
        # force_dry_run=True — ЛІТЕРАЛ, той самий сенс, що й у run_buy_signal_test().
        await _check_position(session, buy_sl, price_sl10, force_dry_run=True)
        remaining = remaining_amount(session, buy_sl)
        if "stop_loss_-10pct" in _triggered_levels(buy_sl):
            lines.append(_ok(f"Рівень -10% спрацював → продано 50% залишку (лишилось {remaining:,.0f} з {buy_sl.token_amount:,.0f})"))
        else:
            lines.append(_fail("Рівень -10% НЕ спрацював (баг у ladder-логіці, дивись position_monitor.py)"))
        lines.append("")

        await asyncio.sleep(QUOTE_CALL_DELAY_SECONDS)
        price_sl20 = entry_price * 0.80
        lines.append(f"Сценарій SL: ціна впала до {format_price_usd(price_sl20)} (-20% від входу)")
        await _check_position(session, buy_sl, price_sl20, force_dry_run=True)
        remaining = remaining_amount(session, buy_sl)
        if "stop_loss_-20pct" in _triggered_levels(buy_sl) and remaining < 1.0:
            lines.append(_ok("Рівень -20% спрацював → продано решту (позиція закрита)"))
        else:
            lines.append(_fail(f"Рівень -20% не спрацював як очікувалось (залишок={remaining:,.0f})"))
        lines.append("")

        # "Скидаємо позицію" — свіжий buy-рядок для сценарію take-profit,
        # незалежний від SL-сценарію вище (та сама вимога в постановці).
        buy_tp = Trade(
            action="buy", token_symbol=TEST_LADDER_TOKEN_SYMBOL,
            contract_address=TEST_LADDER_CONTRACT,
            # chain="solana_test" — див. коментар при створенні buy_sl вище.
            chain="solana_test", amount_usd=10.0, tx_hash="TEST_SIMULATION_NO_TX",
            dry_run=True, status="confirmed",
            entry_price=entry_price, token_amount=1_000_000.0,
            triggered_levels="[]",
        )
        session.add(buy_tp)
        session.commit()

        tp_steps = [
            (0.30, "take_profit_+30pct", "+30%", "продано 30% від початкового обсягу"),
            (0.60, "take_profit_+60pct", "+60%", "продано ще 30% від початкового обсягу"),
            (1.00, "take_profit_+100pct", "+100%", "продано решту (позиція закрита)"),
        ]
        for pct, level_code, label, expect_desc in tp_steps:
            await asyncio.sleep(QUOTE_CALL_DELAY_SECONDS)
            price = entry_price * (1 + pct)
            lines.append(f"Сценарій TP: вхід {format_price_usd(entry_price)} → ціна {format_price_usd(price)} ({label})")
            await _check_position(session, buy_tp, price, force_dry_run=True)
            remaining = remaining_amount(session, buy_tp)
            if level_code in _triggered_levels(buy_tp):
                lines.append(_ok(f"Рівень {label} спрацював → {expect_desc} (залишок {remaining:,.0f})"))
            else:
                lines.append(_fail(f"Рівень {label} НЕ спрацював (баг у ladder-логіці)"))
            lines.append("")

        return lines
    finally:
        try:
            # Прибирання за token_symbol, НЕ за адресою контракту — TEST_LADDER_CONTRACT
            # тепер справжня адреса (Wrapped SOL mint, див. коментар вище), і
            # видаляти за нею було б небезпечно, якби колись з'явився реальний
            # Trade-рядок з тим самим contract_address. Дочірні sell-рядки, які
            # створює execute_partial_sell(), успадковують token_symbol від
            # buy-рядка — тож один фільтр ловить і buy, і всі його sell разом.
            deleted = session.query(Trade).filter(
                Trade.token_symbol == TEST_LADDER_TOKEN_SYMBOL
            ).delete(synchronize_session=False)
            session.commit()
            logger.info(f"🧪 Тест ladder: прибрано {deleted} тестових Trade-рядків")
        except Exception as e:
            logger.warning(f"Не вдалось прибрати тестові Trade-рядки після 🧪 Тест: {e}")
        session.close()
