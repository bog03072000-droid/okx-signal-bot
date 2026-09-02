"""
Розрахунок статистики для кнопки "📊 Статистика" в control-боті: сигнали,
угоди, реалізований PnL, win rate, спрацювання ladder — за обраний період
(день/тиждень/місяць).

PnL береться напряму з Trade.pnl_usd (заповнюється в
core/position_monitor.py:execute_partial_sell() для КОЖНОГО sell, незалежно
від джерела — ladder, reply-sell, прямий sell-сигнал). Тут нічого НЕ
рахується заднім числом для старих sell-рядків без pnl_usd (їх просто не
враховує SUM/AVG, бо там NULL) — старі угоди, закомічені до цієї фічі,
лишаться "недоступними" для PnL-статистики, і це очікувано, а не баг.

Сигнали (SignalLog) не мають dry_run — це поле лише в Trade, тому розділ
"Сигнали" завжди спільний, а розділи "Угоди"/"Результат"/"Ladder" рахуються
ОКРЕМО для dry_run=True і dry_run=False (не змішуються в одну суму).
"""
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func

from core.storage import SignalLog, Trade, TEST_TOKEN_SYMBOL
from core.position_monitor import remaining_amount, POSITION_EPSILON
from core.formatting import display_token_symbol
from core.wallet import MOCK_WALLET_BALANCE_USD

PERIODS = {
    "day": ("День", dt.timedelta(days=1)),
    "week": ("Тиждень", dt.timedelta(days=7)),
    "month": ("Місяць", dt.timedelta(days=30)),
}


def period_start_for(period_key: str, now: Optional[dt.datetime] = None) -> dt.datetime:
    now = now or dt.datetime.utcnow()
    _, delta = PERIODS[period_key]
    return now - delta


@dataclass
class TradeStats:
    opened_positions: int = 0
    closed_fully: int = 0
    still_open: int = 0
    total_pnl_usd: Optional[float] = None
    total_pnl_pct: Optional[float] = None  # None = недоступний (баланс невідомий/нульовий), не 0
    win_rate_pct: Optional[float] = None
    closed_sell_count: int = 0
    winning_sell_count: int = 0
    best_trade: Optional[tuple] = None   # (pct, token_symbol, contract_address)
    worst_trade: Optional[tuple] = None  # (pct, token_symbol, contract_address)
    stop_loss_triggers: int = 0
    take_profit_triggers: int = 0


@dataclass
class SignalStats:
    received: int = 0
    executed_buy: int = 0
    rejected: int = 0
    rejected_screening: int = 0
    rejected_unsupported_network: int = 0
    rejected_risk_limits: int = 0


def _compute_signal_stats(session, period_start: dt.datetime) -> SignalStats:
    s = SignalStats()
    # "Отримано" — скільки повідомлень LLM визнав ТОРГОВИМ СИГНАЛОМ (is_signal=True),
    # а не буквально кожне повідомлення з каналу (флекс/розмови туди не рахуються).
    s.received = session.query(SignalLog).filter(
        SignalLog.created_at >= period_start, SignalLog.is_signal.is_(True)
    ).count()
    s.executed_buy = session.query(SignalLog).filter(
        SignalLog.created_at >= period_start,
        SignalLog.executed.is_(True),
        SignalLog.action == "buy",
    ).count()
    s.rejected = session.query(SignalLog).filter(
        SignalLog.created_at >= period_start,
        SignalLog.is_signal.is_(True),
        SignalLog.rejection_reason.isnot(None),
    ).count()
    s.rejected_screening = session.query(SignalLog).filter(
        SignalLog.created_at >= period_start,
        SignalLog.is_signal.is_(True),
        SignalLog.rejection_reason.like("Токен не пройшов скринінг%"),
    ).count()
    # Структурована колонка chain, не текст rejection_reason — той самий
    # підхід, що вже є в /status (control_bot.py:cmd_status).
    s.rejected_unsupported_network = session.query(SignalLog).filter(
        SignalLog.created_at >= period_start,
        SignalLog.is_signal.is_(True),
        SignalLog.chain.isnot(None),
        SignalLog.chain != "solana",
    ).count()
    # "Через ризик-ліміти" — усе решта відхиленого (пауза, впевненість, cooldown,
    # ліміт позицій, баланс, денний ліміт збитків, помилка quote/price impact,
    # "нема що продавати"). Немає окремих структурованих колонок під кожну з
    # цих причин — рахуємо як залишок, а не окремими LIKE-патернами під кожну
    # (крихкіше було б звірятись з точним форматуванням десятка різних фраз).
    s.rejected_risk_limits = max(0, s.rejected - s.rejected_screening - s.rejected_unsupported_network)
    return s


def _compute_trade_stats(
    session, period_start: dt.datetime, dry_run: bool, balance_usd: Optional[float] = None,
) -> TradeStats:
    """
    balance_usd — база для total_pnl_pct: ПОТОЧНИЙ баланс (mock для dry-run,
    реальний з гаманця для live) на момент виклику /статистика, а НЕ баланс
    на початок обраного періоду (день/тиждень/місяць) — snapshot балансу на
    початок періоду в проєкті ніде не зберігається (перевірено, немає такої
    таблиці/поля), а заводити його заради одного відсотка — непропорційні
    зусилля. "Поточний баланс" — простіший, технічно надійний варіант
    (просто виклик get_wallet_balance(), який вже є), АЛЕ менш точний, якщо
    баланс змінювався за період (поповнення/виведення) — тому текст у
    _format_trade_block() ЯВНО каже "від поточного балансу", а не просто
    "% дохідності", щоб не створювати хибне враження точності, якої немає.
    """
    t = TradeStats()

    # Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL), а НЕ "!=" — token_symbol
    # часто NULL (див. core/formatting.py:display_token_symbol), а звичайне
    # "!=" в SQL повертає UNKNOWN (не True) для NULL-рядків, і вони мовчки
    # випали б із запиту разом з тестовими. isnot() генерує SQL "IS NOT",
    # який у SQLite коректно обробляє NULL (NULL IS NOT 'x' → true).
    opened_buys = session.query(Trade).filter(
        Trade.action == "buy",
        Trade.status == "confirmed",
        Trade.dry_run == dry_run,
        Trade.created_at >= period_start,
        Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL),
    ).all()
    t.opened_positions = len(opened_buys)
    for buy in opened_buys:
        if buy.token_amount and remaining_amount(session, buy) <= POSITION_EPSILON:
            t.closed_fully += 1
    t.still_open = t.opened_positions - t.closed_fully

    sells = session.query(Trade).filter(
        Trade.action == "sell",
        Trade.status == "confirmed",
        Trade.dry_run == dry_run,
        Trade.created_at >= period_start,
        Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL),
    ).all()

    pnl_sells = [s for s in sells if s.pnl_usd is not None]
    if pnl_sells:
        t.total_pnl_usd = sum(s.pnl_usd for s in pnl_sells)
        # balance_usd може бути None (баланс невідомий, напр. RPC недоступний)
        # або 0 — в обох випадках НЕ ділимо, лишаємо total_pnl_pct=None, а не
        # inf/NaN чи хибний 0%.
        if balance_usd:
            t.total_pnl_pct = t.total_pnl_usd / balance_usd * 100
        t.closed_sell_count = len(pnl_sells)
        t.winning_sell_count = sum(1 for s in pnl_sells if s.pnl_usd > 0)
        t.win_rate_pct = (t.winning_sell_count / t.closed_sell_count) * 100

        best = None
        worst = None
        for s in pnl_sells:
            cost_basis = s.amount_usd - s.pnl_usd
            if cost_basis == 0:
                continue
            pct = s.pnl_usd / cost_basis * 100
            if best is None or pct > best[0]:
                best = (pct, s.token_symbol, s.contract_address)
            if worst is None or pct < worst[0]:
                worst = (pct, s.token_symbol, s.contract_address)
        t.best_trade = best
        t.worst_trade = worst

    t.stop_loss_triggers = sum(1 for s in sells if s.close_reason and s.close_reason.startswith("stop_loss_"))
    t.take_profit_triggers = sum(1 for s in sells if s.close_reason and s.close_reason.startswith("take_profit_"))

    return t


def _format_trade_block(label: str, t: TradeStats, balance_label: str) -> str:
    lines = [f"{label} Угоди:"]
    lines.append(f"• Відкрито позицій: {t.opened_positions}")
    lines.append(f"• Закрито повністю: {t.closed_fully}")
    lines.append(f"• Досі відкрито: {t.still_open}")
    lines.append("")
    lines.append(f"{label} Результат:")
    if t.total_pnl_usd is None:
        lines.append("• PnL поки не відстежується (немає закритих угод із розрахованим pnl_usd за цей період)")
    else:
        pct_note = f" ({t.total_pnl_pct:+.1f}% від {balance_label})" if t.total_pnl_pct is not None else " (% недоступний — баланс невідомий)"
        lines.append(f"• Сумарний PnL: ${t.total_pnl_usd:+.2f}{pct_note}")
        lines.append(f"• Win rate: {t.win_rate_pct:.0f}% ({t.winning_sell_count} з {t.closed_sell_count} закритих)")
        if t.best_trade:
            lines.append(f"• Найкраща угода: {t.best_trade[0]:+.1f}% ({display_token_symbol(t.best_trade[1], t.best_trade[2])})")
        if t.worst_trade:
            lines.append(f"• Найгірша угода: {t.worst_trade[0]:+.1f}% ({display_token_symbol(t.worst_trade[1], t.worst_trade[2])})")
    lines.append("")
    lines.append(f"{label} Спрацювання ladder:")
    lines.append(f"• Stop-loss спрацювань: {t.stop_loss_triggers}")
    lines.append(f"• Take-profit спрацювань: {t.take_profit_triggers}")
    return "\n".join(lines)


def format_stats_report(session, period_key: str, live_wallet_usdt_balance: Optional[float] = None) -> str:
    """
    live_wallet_usdt_balance — ПОТОЧНИЙ реальний баланс USDT гаманця (не за
    цей виклик рахується — передається готовим із control_bot.py, де його
    отримують через asyncio.to_thread(get_wallet_balance) ДО виклику цієї
    функції, щоб сам format_stats_report() лишався синхронним і легко
    тестованим, без мережевого виклику всередині). None — якщо реальний
    баланс недоступний (RPC впав, ключ не задано тощо); у такому разі
    відсоток PnL для LIVE-секції показується як "недоступний", а не 0/inf.

    Для DRY RUN базою для % завжди служить MOCK_WALLET_BALANCE_USD (core/
    wallet.py) — той самий умовний капітал, від якого dry-run угоди й
    рахують розмір позиції, а не будь-який реальний баланс гаманця,
    навіть якщо він налаштований.
    """
    period_label, _ = PERIODS[period_key]
    period_start = period_start_for(period_key)

    sig = _compute_signal_stats(session, period_start)
    dry_stats = _compute_trade_stats(session, period_start, dry_run=True, balance_usd=MOCK_WALLET_BALANCE_USD)
    live_stats = _compute_trade_stats(session, period_start, dry_run=False, balance_usd=live_wallet_usdt_balance)

    lines = [f"📊 <b>Статистика за {period_label.lower()}</b>", ""]
    lines.append("Сигнали:")
    lines.append(f"• Отримано: {sig.received}")
    lines.append(f"• Виконано (куплено): {sig.executed_buy}")
    lines.append(f"• Відхилено: {sig.rejected}")
    lines.append(f"  — з них через скринінг: {sig.rejected_screening}")
    lines.append(f"  — через ризик-ліміти: {sig.rejected_risk_limits}")
    lines.append(f"  — непідтримувана мережа: {sig.rejected_unsupported_network}")
    lines.append("")
    lines.append(_format_trade_block("🧪 DRY RUN", dry_stats, balance_label="поточного mock-балансу драй-рану"))
    lines.append("")
    lines.append(_format_trade_block("🔴 LIVE", live_stats, balance_label="поточного балансу гаманця"))

    return "\n".join(lines)
