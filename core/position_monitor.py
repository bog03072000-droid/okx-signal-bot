"""
Фоновий моніторинг відкритих позицій: сходинковий (ladder) stop-loss/take-profit,
незалежно від сигналів з тг-каналу. Працює як окрема asyncio-задача в тому
самому event loop, що й Telethon listener, control-бот і heartbeat
(main.py, asyncio.gather) — без окремого процесу.

Ladder-рівні (кожен спрацьовує РІВНО ОДИН РАЗ на позицію; і SL, і TP рахуються
ЗАВЖДИ від entry_price, а НЕ від піку ціни й НЕ від попереднього рівня —
навіть якщо частина позиції вже продана по take-profit, а потім ціна впаде
нижче входу, stop-loss все одно рахується від початкової ціни входу):

  Stop-loss (падіння від входу):
    -10%  → продати 50% залишку, що є НА ЦЕЙ МОМЕНТ (не від початкового обсягу)
    -20%  → продати 100% залишку (усе, що лишилось)

  Take-profit (зростання від входу):
    +30%  → продати 30% ПОЧАТКОВОГО обсягу (entry token_amount)
    +60%  → продати ще 30% ПОЧАТКОВОГО обсягу
    +100% → продати решту (100% залишку на цей момент)

Батчинг цін: core/price_feed.py — один запит на цикл на ВСІ відкриті позиції
одразу (до 30 адрес за раз), а не по окремому запиту на кожну позицію —
щоб не наразитись на rate limit DexScreener навіть при MAX_OPEN_POSITIONS
і 2-секундному інтервалі перевірки.
"""
import asyncio
import json
import logging
import time

from core.config import settings, get_limit
from core.okx_dex_client import OKXDexClient, USDT_MINT_SOLANA, USDT_DECIMALS
from core.storage import get_session, Trade
from core.price_feed import fetch_prices_usd
from core.control_bot import notify_owner
from core.formatting import format_price_usd, display_token_symbol

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 2
# Додатковий запобіжник поверх батчингу — навіть при MAX_OPEN_POSITIONS=5 і
# циклі раз/2с це зводить реальні HTTP-запити до DexScreener приблизно
# до 1 на ~4с (~15/хв, свіжий запит раз на ~2 цикли), а не раз/цикл — з
# великим запасом. Заодно узгоджено з тим, що сам DexScreener кешує на
# своєму CDN ~30с (див. core/price_feed.py) — частіші запити все одно не
# дають нової інформації.
PRICE_CACHE_TTL_SECONDS = 4
POSITION_EPSILON = 1e-6  # нижче цього — вважаємо позицію повністю закритою
PCT_EPSILON = 1e-9  # допуск на похибку float при порівнянні % зміни з порогом

# (код_рівня, поріг_%_від_входу, база_розрахунку, частка)
#   база "remaining" — частка від залишку НА ЦЕЙ МОМЕНТ (token_amount мінус
#     усе вже продане по цій позиції);
#   база "initial"   — частка від ПОЧАТКОВОГО обсягу (buy.token_amount).
# Список впорядкований від найм'якшого до найжорсткішого рівня — порядок
# важливий: якщо ціна одразу проскочила кілька порогів за один цикл (напр.
# -25% за секунду), рівні мають спрацювати послідовно один за одним, а не
# лише найжорсткіший.
STOP_LOSS_LEVELS = [
    ("stop_loss_-10pct", -0.10, "remaining", 0.50),
    ("stop_loss_-20pct", -0.20, "remaining", 1.00),
]
TAKE_PROFIT_LEVELS = [
    ("take_profit_+30pct", 0.30, "initial", 0.30),
    ("take_profit_+60pct", 0.60, "initial", 0.30),
    ("take_profit_+100pct", 1.00, "remaining", 1.00),
]
ALL_LEVELS = STOP_LOSS_LEVELS + TAKE_PROFIT_LEVELS

dex = OKXDexClient()

_price_cache: dict[str, float] = {}
_price_cache_updated_at: float = 0.0
_last_checked_price: dict[int, float] = {}  # buy.id -> остання оброблена ціна

# П.1 (повторний аудит): execute_partial_sell() викликається з ТРЬОХ незалежних
# джерел (ladder _check_position нижче, прямий sell-сигнал і reply-sell — обидва
# в main.py) без синхронізації — два одночасні виклики на ТУ САМУ позицію могли
# прочитати однаковий remaining_amount() і обидва спробувати продати весь
# залишок. Lock ПЕР-ПОЗИЦІЯ (ключ — buy.id), а не один спільний lock на все:
# інакше продаж однієї позиції зайве блокував би перевірку/продаж УСІХ інших.
#
# Очищення: свідомо НЕ видаляємо записи зі словника навіть коли позиція
# повністю закрита. Видалення було б racy (інша задача могла б уже отримати
# посилання на СТАРИЙ Lock-об'єкт з _position_locks.get(), поки ми його
# видаляємо і хтось третій створює НОВИЙ Lock під тим самим ключем — тоді два
# "паралельних" викликачі тримали б РІЗНІ lock-об'єкти і взаємно не бачили б
# одне одного, що зводить нанівець весь сенс лока). Пам'ять на це тривіальна:
# один asyncio.Lock() — це лише кілька байт, а buy.id — цілі позиції, яких
# для соло-бота реалістично сотні-тисячі за роки роботи, не мільйони.
_position_locks: dict[int, asyncio.Lock] = {}


def _lock_for(buy_id: int) -> asyncio.Lock:
    lock = _position_locks.get(buy_id)
    if lock is None:
        lock = asyncio.Lock()
        _position_locks[buy_id] = lock
    return lock


# Лічильник послідовних divergence-блокувань на buy.id (реалізація TODO,
# який раніше тут стояв незакритим). Спостережено наживо: позиція може
# застрягти на годину+ з розбіжністю в рази вище порогу (типово — токен
# втратив ліквідність/рагнувся, і OKX quote більше не відображає ціну,
# яку показує DexScreener) — guard коректно НЕ дає продати по поганій
# ціні, але без цього лічильника власник дізнавався б про застрягання
# лише за шквалом однакових сповіщень щоцикл (кожні 2с), без чіткого
# сигналу "це вже не шум, треба дивитись руками".
_divergence_block_counts: dict[int, int] = {}
STUCK_DIVERGENCE_THRESHOLD_CYCLES = 5  # ескалація що кожні N послідовних блокувань


def _get_open_positions(session) -> list:
    """
    Відкрита позиція — buy Trade(status=confirmed) з відомими token_amount і
    entry_price, де залишок (token_amount мінус сума token_amount дочірніх
    confirmed sell) більший за POSITION_EPSILON.

    Buy-рядки без entry_price (lookup ціни при купівлі не вдався) свідомо
    ІГНОРУЮТЬСЯ тут — без точки відліку ladder не може порахувати % зміни,
    і краще мовчки не моніторити позицію, ніж рахувати від хибного нуля.
    """
    buys = session.query(Trade).filter(
        Trade.action == "buy",
        Trade.status == "confirmed",
        Trade.chain == "solana",
        Trade.token_amount.isnot(None),
        Trade.entry_price.isnot(None),
    ).all()

    open_positions = []
    for buy in buys:
        if remaining_amount(session, buy) > POSITION_EPSILON:
            open_positions.append(buy)
    return open_positions


def _triggered_levels(buy: Trade) -> set:
    try:
        return set(json.loads(buy.triggered_levels or "[]"))
    except (json.JSONDecodeError, TypeError):
        return set()


def _mark_triggered(buy: Trade, level_code: str):
    levels = _triggered_levels(buy)
    levels.add(level_code)
    buy.triggered_levels = json.dumps(sorted(levels))


def remaining_amount(session, buy: Trade) -> float:
    sold = session.query(Trade).filter(
        Trade.parent_trade_id == buy.id,
        Trade.status == "confirmed",
    ).all()
    sold_amount = sum(s.token_amount or 0.0 for s in sold)
    return (buy.token_amount or 0.0) - sold_amount


async def execute_partial_sell(
    session, buy: Trade, close_reason: str, sell_raw_amount: float, current_price: float = None,
    force_dry_run: bool = False,
) -> bool:
    """
    Продає sell_raw_amount (у найменших одиницях токена) з позиції buy,
    записує Trade (з pnl_usd, див. нижче) і сповіщає власника. Спільна для
    ЧОТИРЬОХ джерел (звідси і галузі label/source_note нижче за close_reason):
      - ladder TP/SL (_check_position нижче, close_reason напр. "stop_loss_-10pct")
      - прямий sell-сигнал з каналу (main.py:process_signal(), close_reason
        "signal_sell" — контракт вказано явно в тексті сигналу)
      - best-effort reply-sell евристики (main.py:process_reply_sell(),
        close_reason "reply_sell_heuristic_Xpct" — sell-згадка в reply без CA)
      - кнопки "🧪 Тест" (core/self_test.py, force_dry_run=True) — той самий
        код ladder-логіки, форсовано без реального свопу незалежно від
        settings.dry_run, див. docstring OKXDexClient.execute_swap().

    current_price — якщо відомий, у сповіщенні показується % зміни від
    entry_price; якщо None (напр. свіжий price lookup не вдався) — без %.

    Повертає True, якщо своп реально виконався (dry-run теж рахується як
    "виконався" — просто без реальної транзакції в мережі).
    """
    if sell_raw_amount <= 0:
        return False

    # П.1 (повторний аудит): весь блок "перевір залишок → визнач суму
    # продажу → pending-рядок → своп → фінальний коміт" — під локом НА ЦЮ
    # КОНКРЕТНУ ПОЗИЦІЮ (buy.id). Тримаємо лок аж до фінального
    # session.commit() (а не лише навколо читання/запису pending-рядка):
    # remaining_amount() рахує лише status=="confirmed" sell-рядки, тому
    # ЧУЖИЙ pending sell (ще не підтверджений) для неї "невидимий" — якби
    # лок звільнявся раніше, другий паралельний виклик міг би прочитати
    # remaining_amount(), що ще не враховує перший (не підтверджений) sell,
    # і знову спробувати продати той самий залишок.
    async with _lock_for(buy.id):
        # Перевіряємо залишок ЗАНОВО під локом — інший виклик (ladder/прямий
        # sell-сигнал/reply-sell) міг продати частину чи все, поки цей чекав
        # на лок. Якщо вже нічого продавати — тихо виходимо, це НЕ помилка.
        fresh_remaining = remaining_amount(session, buy)
        if fresh_remaining <= POSITION_EPSILON:
            logger.info(
                f"{close_reason}: позиція {buy.token_symbol} вже повністю продана іншим "
                f"паралельним викликом (поки чекали на lock) — пропускаємо"
            )
            return False
        # Клемпимо запитану суму до РЕАЛЬНОГО поточного залишку — викликач
        # (ladder/main.py) рахував sell_raw_amount ДО того, як зайняв лок,
        # тож міг орієнтуватись на вже застарілий remaining_amount().
        sell_raw_amount = min(sell_raw_amount, fresh_remaining)

        amount_raw = str(int(sell_raw_amount))

        # Синхронні HTTP-виклики (get_quote/execute_swap — той самий шлях, що й
        # для sell-сигналів з каналу в main.py) винесені в окремий потік через
        # asyncio.to_thread, щоб НЕ блокувати спільний event loop (Telethon
        # listener + control-бот сидять в ньому ж). На відміну від main.py, де
        # блокуючий виклик стається раз на сигнал, тут цикл раз/2с — тому
        # блокування тут відчутніше для чутливості control-бота на команди.
        quote = await asyncio.to_thread(dex.get_quote, buy.contract_address, USDT_MINT_SOLANA, amount_raw)
        if not quote.success:
            logger.warning(f"{close_reason}: помилка quote для {buy.token_symbol}: {quote.error}")
            return False

        # amount_usd — з quote.to_amount (реальна кількість USDT за курсом свопу),
        # а не sell_raw_amount * current_price (DexScreener-ціна) — курс свопу і
        # ринкова ціна DexScreener можуть трохи розходитись (price impact, спред).
        # Рахуємо ДО execute_swap(), бо нижче він же потрібен для перевірки
        # розбіжності цін (MAX_PRICE_DIVERGENCE_PCT) — перед виконанням свопу,
        # а не після.
        try:
            amount_usd = float(quote.to_amount) / (10 ** USDT_DECIMALS) if quote.to_amount else 0.0
        except (TypeError, ValueError):
            amount_usd = 0.0

        # --- Перевірка розбіжності: DexScreener-ціна тригера vs реальна ціна OKX ---
        # Стосується ЛИШЕ автоматичних спрацювань ladder TP/SL (close_reason з
        # префіксом stop_loss_/take_profit_) — прямі sell-сигнали з каналу і
        # reply-sell евристики цю перевірку НЕ проходять (у них немає "тригера
        # за іншою ціною", який можна було б звірити — юзер сам вирішив продати).
        # Це ОКРЕМА перевірка від check_price_impact (core/risk_manager.py) —
        # та стосується власного price impact ОДНОГО свопу, а не розбіжності
        # МІЖ двома різними джерелами ціни (DexScreener проти OKX).
        #
        # Через відсутність decimals токена в цій системі (token_amount/
        # sell_raw_amount — сирі, найменші одиниці; entry_price/current_price —
        # USD за ЦІЛИЙ токен з DexScreener) пряме порівняння "ціна_OKX $ проти
        # ціна_DexScreener $" неможливе без знання decimals. Замість цього
        # порівнюємо RATIO: "% зміни ціни, який неявно випливає з реального
        # котирування OKX" (окремо для собівартості на raw-юніт, той самий
        # decimals-незалежний підхід, що вже в pnl_usd нижче) проти "% зміни
        # ціни, яку показав DexScreener і яка викликала тригер" (pct_change).
        # Математично еквівалентно порівнянню абсолютних цін, бо обидві сторони
        # виражені відносно ОДНІЄЇ й тієї самої точки відліку (вхід у позицію).
        # force_dry_run виключено з цієї перевірки: це ЛИШЕ кнопка "🧪 Тест"
        # (core/self_test.py) — вона свідомо використовує синтетичні
        # entry_price/amount_usd (напр. $10 за 1,000,000 raw-юнітів) проти
        # РЕАЛЬНОГО OKX quote на справжній Wrapped SOL, тож "очікувана" ціна за
        # цими фейковими числами не має нічого спільного з реальним котируванням
        # — розбіжність завжди була б величезною і блокувала б КОЖЕН рівень
        # тесту. Оскільки force_dry_run=True вже гарантує, що жодного реального
        # свопу не буде (execute_swap завжди повертає симуляцію), захищати тут
        # нічого — перевірка існує для РЕАЛЬНИХ грошей, а не для тесту логіки
        # порогів.
        is_ladder_trigger = close_reason.startswith("stop_loss_") or close_reason.startswith("take_profit_")
        if (
            is_ladder_trigger and not force_dry_run
            and current_price is not None and buy.entry_price and buy.amount_usd and buy.token_amount
        ):
            pct_change_dexscreener = (current_price - buy.entry_price) / buy.entry_price
            cost_per_raw_unit = buy.amount_usd / buy.token_amount
            expected_price_per_raw_unit = cost_per_raw_unit * (1 + pct_change_dexscreener)
            actual_price_per_raw_unit = amount_usd / sell_raw_amount
            if expected_price_per_raw_unit != 0:
                divergence_pct = abs(
                    actual_price_per_raw_unit - expected_price_per_raw_unit
                ) / abs(expected_price_per_raw_unit) * 100
                max_divergence_pct = get_limit("MAX_PRICE_DIVERGENCE_PCT")
                if divergence_pct > max_divergence_pct:
                    logger.warning(
                        f"{close_reason}: розбіжність цін {divergence_pct:.1f}% (DexScreener-тригер "
                        f"vs реальний OKX quote) перевищує ліміт {max_divergence_pct}% для "
                        f"{buy.token_symbol} — своп ПРИЗУПИНЕНО на цьому циклі, рівень НЕ позначено "
                        f"спрацьованим, перевірка повториться наступного циклу."
                    )
                    await notify_owner(
                        f"⚠️ Розбіжність цін {divergence_pct:.1f}% перевищує ліміт "
                        f"{max_divergence_pct}% для {display_token_symbol(buy.token_symbol, buy.contract_address)} "
                        f"— своп призупинено, рівень {close_reason} буде перевірено повторно "
                        f"наступного циклу."
                    )
                    # Лічильник послідовних блокувань на ЦЮ позицію — див.
                    # docstring _divergence_block_counts вище. Ескалуємо
                    # окремим, чіткіше сформульованим сповіщенням раз на
                    # STUCK_DIVERGENCE_THRESHOLD_CYCLES (а не щоцикл, як
                    # звичайне попередження вище) — інакше власник просто
                    # потоне в однакових паперових попередженнях і не
                    # відрізнить "разова волатильність" від "застрягло".
                    block_count = _divergence_block_counts.get(buy.id, 0) + 1
                    _divergence_block_counts[buy.id] = block_count
                    if block_count % STUCK_DIVERGENCE_THRESHOLD_CYCLES == 0:
                        stuck_seconds = block_count * CHECK_INTERVAL_SECONDS
                        await notify_owner(
                            f"🆘 Позиція {display_token_symbol(buy.token_symbol, buy.contract_address)} "
                            f"застрягла: розбіжність цін перевищує ліміт вже {block_count} циклів "
                            f"поспіль (≈{stuck_seconds}с). Схоже на втрату ліквідності токена "
                            f"(rug/делістинг), а не тимчасовий шум — перевір токен вручну "
                            f"(DexScreener/Solscan). Якщо ціна легітимна — тимчасово підніми "
                            f"MAX_PRICE_DIVERGENCE_PCT через /setlimit, щоб дати свопу пройти."
                        )
                    return False
                # Розбіжність в межах порогу — позиція більше "не застрягла",
                # скидаємо лічильник послідовних блокувань.
                _divergence_block_counts.pop(buy.id, None)

        # --- PnL для ЦІЄЇ конкретної частки, що продається ---
        # cost_per_raw_unit = скільки USD коштував ОДИН raw-юніт токена на вході
        # (buy.amount_usd / buy.token_amount) — decimals довільного токена
        # скорочуються в цьому співвідношенні, тому не треба їх окремо знати.
        # pnl = виручка за цю частку - собівартість цієї ж частки. НЕ враховує
        # комісії мережі/сліпедж понад те, що вже відображено в amount_usd з
        # реального quote — окремих даних про комісії система не збирає.
        # Рахуємо ДО execute_swap() — amount_usd (з quote) вже відомий, і pnl_usd
        # можна одразу покласти в pending-рядок нижче.
        pnl_usd = None
        if buy.amount_usd is not None and buy.token_amount:
            cost_basis_usd = (buy.amount_usd / buy.token_amount) * sell_raw_amount
            pnl_usd = amount_usd - cost_basis_usd

        # --- Pending-запис ДО свопу (П.2) ---
        # Якщо процес впаде між відправкою транзакції і записом результату —
        # без цього кроку бот "загубив" би позицію (гроші реально рухались, а в
        # БД жодного сліду). status="pending" рядок хоча б лишається видимим і
        # /status показує його як "застряглий" після 5хв.
        sell_trade = Trade(
            action="sell",
            token_symbol=buy.token_symbol,
            contract_address=buy.contract_address,
            chain=buy.chain,
            amount_usd=amount_usd,
            pnl_usd=pnl_usd,
            token_amount=sell_raw_amount,
            dry_run=force_dry_run or settings.dry_run,
            status="pending",
            parent_trade_id=buy.id,
            close_reason=close_reason,
        )
        session.add(sell_trade)
        session.commit()

        swap_result = await asyncio.to_thread(
            dex.execute_swap,
            buy.contract_address, USDT_MINT_SOLANA, amount_raw,
            wallet_address="<буде підставлено з гаманця>",
            slippage_pct=settings.max_slippage_pct,
            chain_id="501",
            force_dry_run=force_dry_run,
        )

        # --- Оновлюємо ТОЙ САМИЙ pending-рядок (не створюємо новий) ---
        sell_trade.tx_hash = swap_result.tx_hash
        sell_trade.dry_run = swap_result.dry_run
        sell_trade.status = "confirmed" if swap_result.success else "failed"
        if not swap_result.success:
            sell_trade.failure_reason = swap_result.error
        session.add(sell_trade)

        if swap_result.success:
            _mark_triggered(buy, close_reason)
        else:
            # Своп не вдався — НЕ позначаємо рівень як спрацьований: для ladder-
            # рівнів це дозволяє спробувати ще раз наступного циклу (раз/2с);
            # для reply-sell це нейтрально (повторного виклику однаково не буде).
            logger.warning(f"{close_reason}: своп для {buy.token_symbol} не вдався: {swap_result.error}")

        session.add(buy)
        session.commit()
    # --- lock звільнено тут (async with вище) — усе, що нижче, вже не
    # стосується узгодження залишку, тільки формування сповіщення ---

    # close_reason визначає, звідки прийшов цей sell — три джерела ділять
    # цю саму функцію (ladder, reply-sell евристика, прямий sell-сигнал з
    # каналу), тому підпис у сповіщенні має відрізнятись, щоб було видно,
    # який саме механізм спрацював.
    if close_reason.startswith("stop_loss_") or close_reason.startswith("take_profit_"):
        label = f"АВТО ({close_reason})"
        source_note = "автоматичне спрацювання ladder TP/SL"
        closing_note = "<b>НЕ сигнал з каналу</b>"
    elif close_reason.startswith("reply_sell_heuristic_"):
        label = f"REPLY-SELL ({close_reason})"
        source_note = "best-effort розпізнавання sell у reply-повідомленні без адреси контракту (не 100% точний механізм)"
        closing_note = "<b>НЕ прямий сигнал з каналу</b>"
    else:
        label = f"СИГНАЛ ({close_reason})"
        source_note = "прямий sell-сигнал з каналу (адреса контракту вказана явно в тексті)"
        closing_note = "<b>сигнал з каналу</b>"

    # format_price_usd() — ЛИШЕ для показу в тексті сповіщення; сам pct
    # тут і далі (передача в _mark_triggered/порогові порівняння) завжди
    # рахується з "сирих" float entry_price/current_price, а не з цього
    # форматованого рядка. Без format_price_usd() ціни memecoin з 5-10
    # нулями після коми (напр. $0.0000001234) в наївному f"${p:.2f}" завжди
    # показували б "$0.00" — усі значущі цифри губились би.
    pct_note = ""
    if current_price is not None and buy.entry_price:
        pct = (current_price - buy.entry_price) / buy.entry_price * 100
        pct_note = (
            f" ({format_price_usd(buy.entry_price)} → {format_price_usd(current_price)}, "
            f"{pct:+.1f}% від входу)"
        )

    prefix = "🧪 [DRY RUN] " if swap_result.dry_run else "✅ "
    status_note = "" if swap_result.success else " ⚠️ СВОП НЕ ВДАВСЯ"
    text = (
        f"{prefix}📐 <b>{label}</b>: SELL {display_token_symbol(buy.token_symbol, buy.contract_address)} "
        f"≈${amount_usd:.2f}{pct_note}{status_note}\n"
        f"tx: {swap_result.tx_hash}\n"
        f"Це {source_note}, {closing_note}."
    )
    logger.info(text)
    await notify_owner(text)
    return swap_result.success


async def _check_position(session, buy: Trade, current_price: float, force_dry_run: bool = False):
    """
    force_dry_run — прокидається напряму в execute_partial_sell()/execute_swap();
    True лише коли викликається з core/self_test.py (кнопка "🧪 Тест"). За
    замовчуванням False — реальний position_monitor_loop() його не передає
    взагалі, тож поведінка проду абсолютно незмінна.
    """
    triggered = _triggered_levels(buy)
    pct_change = (current_price - buy.entry_price) / buy.entry_price

    for level_code, threshold, basis, fraction in ALL_LEVELS:
        if level_code in triggered:
            continue

        is_stop_loss = threshold < 0
        # PCT_EPSILON: без нього рівень міг НЕ спрацювати на порозі рівно
        # -10.000000% через звичайне округлення float (перевірено: ділення
        # може дати -9.999999999999994% замість -10.0%, і строге "<=" це
        # пропускає). На реальних ринкових цінах така точна межа теж
        # трапляється частіше, ніж здається — краще спрацювати на волосок
        # раніше, ніж пропустити рівень через похибку double.
        crossed = pct_change <= threshold + PCT_EPSILON if is_stop_loss else pct_change >= threshold - PCT_EPSILON
        if not crossed:
            continue

        remaining = remaining_amount(session, buy)
        if remaining <= POSITION_EPSILON:
            # Позиція вже повністю закрита попереднім рівнем в цьому ж циклі —
            # позначаємо рівень як "спрацьований" (нема чого продавати), щоб
            # наступного разу знову не намагався його виконати.
            _mark_triggered(buy, level_code)
            session.add(buy)
            session.commit()
            continue

        base_amount = (buy.token_amount or 0.0) if basis == "initial" else remaining
        sell_amount = min(base_amount * fraction, remaining)

        await execute_partial_sell(session, buy, level_code, sell_amount, current_price, force_dry_run=force_dry_run)


async def position_monitor_loop():
    """
    Раз/2с (CHECK_INTERVAL_SECONDS): збирає всі відкриті позиції, батч-запитом (до 30 адрес за
    раз, TTL-кеш PRICE_CACHE_TTL_SECONDS) отримує ціни з DexScreener,
    перевіряє ladder-рівні й продає частками там, де треба.
    """
    global _price_cache, _price_cache_updated_at

    while True:
        try:
            session = get_session()
            try:
                open_positions = _get_open_positions(session)

                # прибираємо кеш останньої ціни для позицій, які вже закриті —
                # інакше словник неконтрольовано росте з часом
                open_ids = {p.id for p in open_positions}
                for stale_id in [pid for pid in _last_checked_price if pid not in open_ids]:
                    del _last_checked_price[stale_id]

                if open_positions:
                    addresses = list({p.contract_address for p in open_positions})

                    now = time.time()
                    if now - _price_cache_updated_at >= PRICE_CACHE_TTL_SECONDS:
                        _price_cache = await asyncio.to_thread(fetch_prices_usd, addresses, "solana")
                        _price_cache_updated_at = now

                    for buy in open_positions:
                        price = _price_cache.get(buy.contract_address)
                        if price is None:
                            continue
                        if _last_checked_price.get(buy.id) == price:
                            # Ціна не змінилась з попередньої перевірки — пропускаємо
                            # повторний розрахунок/спробу свопу для цієї позиції.
                            continue
                        _last_checked_price[buy.id] = price
                        await _check_position(session, buy, price)
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Помилка в position_monitor_loop: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
