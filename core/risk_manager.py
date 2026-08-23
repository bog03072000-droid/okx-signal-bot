"""
Централізована перевірка ризик-лімітів перед кожною угодою.
Це "останній бар'єр" перед реальним свопом — жодна угода не має його оминати.
"""
import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import func

from core import runtime_state
from core.config import get_limit, settings
from core.storage import get_session, Trade, TEST_TOKEN_SYMBOL

logger = logging.getLogger(__name__)

_last_trade_time: dict[str, float] = {}  # для cooldown, ключ - "chain"


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str = ""


class RiskManager:
    def check_paused(self) -> RiskCheckResult:
        """
        Перевірка прапорця паузи, що виставляється командою /stop в control-боті
        (core/control_bot.py) і знімається командою /start. Це навмисно окрема,
        дуже дешева перевірка на самому початку ланцюжка — щоб /stop реально
        блокував угоди на рівні risk_manager, а не тільки "вимикав" тг-бота.
        """
        if runtime_state.is_paused():
            return RiskCheckResult(
                allowed=False,
                reason="Торгівля призупинена командою /stop. Використай /start в control-боті, щоб відновити.",
            )
        return RiskCheckResult(allowed=True)

    def check_daily_loss_limit(self, wallet_balance_usd: float) -> RiskCheckResult:
        """
        Раніше рахував PnL по ВСІХ Trade за сьогодні, без розрізнення
        dry_run/live чи тестових записів від кнопки "🧪 Тест" — фейковий
        PnL з dry-run чи з тесту міг або хибно зупинити live-торгівлю,
        або, гірше, ЗАМАСКУВАТИ реальний живий збиток. Джерело правди для
        "це тестовий запис" — саме Trade.token_symbol == TEST_TOKEN_SYMBOL
        (core/storage.py): кнопка "🧪 Тест" (core/self_test.py) проставляє
        цю позначку і на buy-, і на всіх дочірніх sell-рядках (sell успадковує
        token_symbol від buy у execute_partial_sell), тож саме цього поля
        достатньо — окремо звіряти ще й chain=="solana_test" тут не треба:
        та позначка існує ЛИШЕ для того, щоб реальний position_monitor_loop
        (core/position_monitor.py:_get_open_positions, фільтр chain=="solana")
        не підхоплював тестові позиції, а не для ідентифікації тестових
        записів як таких.
        """
        session = get_session()
        try:
            today_start = dt.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            total_pnl = session.query(func.sum(Trade.pnl_usd)).filter(
                Trade.created_at >= today_start,
                Trade.pnl_usd.isnot(None),
                Trade.dry_run == settings.dry_run,
                Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL),
            ).scalar() or 0.0

            loss_limit_pct = get_limit("DAILY_LOSS_LIMIT_PCT")
            loss_pct = (-total_pnl / wallet_balance_usd * 100) if wallet_balance_usd > 0 else 0
            if total_pnl < 0 and loss_pct >= loss_limit_pct:
                return RiskCheckResult(
                    allowed=False,
                    reason=(
                        f"Денний ліміт збитків досягнуто: -{loss_pct:.1f}% "
                        f"(ліміт {loss_limit_pct}%). Бот призупинено."
                    ),
                )
            return RiskCheckResult(allowed=True)
        finally:
            session.close()

    def check_open_positions_limit(self) -> RiskCheckResult:
        """
        .filter(Trade.dry_run == settings.dry_run) і .isnot(TEST_TOKEN_SYMBOL) —
        той самий фікс і те саме обґрунтування, що в check_daily_loss_limit()
        вище: без цього старі dry-run позиції (з періоду до переходу на live)
        назавжди "займали" б слоти MAX_OPEN_POSITIONS у live-режимі.

        status IN ("confirmed", "pending") для buy (НЕ лише "confirmed") —
        pending-рядок (core/main.py, OPEN_POSITIONS_LOCK) МАЄ рахуватись як
        зайнятий слот: саме факт його існування — це і є атомарне
        "застовбення" слота під час резервування. Якби рахувався лише
        "confirmed", лок навколо "перевір → створи pending" нічого не
        захищав би — другий паралельний виклик все одно побачив би старий
        (не оновлений) open_count, поки перший ще виконує своп. Для sell
        лишаємо тільки "confirmed" — pending sell ще не звільнив позицію.
        """
        session = get_session()
        try:
            open_count = session.query(Trade).filter(
                Trade.action == "buy", Trade.status.in_(("confirmed", "pending")),
                Trade.dry_run == settings.dry_run,
                Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL),
            ).count()
            closed_count = session.query(Trade).filter(
                Trade.action == "sell", Trade.status == "confirmed",
                Trade.dry_run == settings.dry_run,
                Trade.token_symbol.isnot(TEST_TOKEN_SYMBOL),
            ).count()
            net_open = open_count - closed_count
            max_open = get_limit("MAX_OPEN_POSITIONS")
            if net_open >= max_open:
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Досягнуто ліміту відкритих позицій ({max_open})",
                )
            return RiskCheckResult(allowed=True)
        finally:
            session.close()

    def check_cooldown(self, chain: str) -> RiskCheckResult:
        import time
        cooldown_seconds = get_limit("TRADE_COOLDOWN_SECONDS")
        last = _last_trade_time.get(chain, 0)
        elapsed = time.time() - last
        if elapsed < cooldown_seconds:
            return RiskCheckResult(
                allowed=False,
                reason=f"Cooldown: залишилось {cooldown_seconds - elapsed:.0f}с",
            )
        return RiskCheckResult(allowed=True)

    def register_trade_time(self, chain: str):
        import time
        _last_trade_time[chain] = time.time()

    def calculate_position_size(self, wallet_balance_usd: float) -> float:
        """Фіксований % від балансу гаманця на одну угоду."""
        return wallet_balance_usd * (get_limit("MAX_POSITION_PCT") / 100)

    def check_price_impact(self, price_impact_pct: float) -> RiskCheckResult:
        """
        price_impact_pct приходить з OKXDexClient.get_quote() у знаку OKX V6:
        ДОДАТНЄ = отримав БІЛЬШЕ за очікуване (вигідно, ніколи не привід
        відхиляти), ВІД'ЄМНЕ = отримав МЕНШЕ за очікуване (класичний price
        impact, ось це і обмежуємо). Тому поріг MAX_PRICE_IMPACT_PCT (завжди
        задається додатним числом в .env, напр. 5.0) звіряється з
        -price_impact_pct, а НЕ з price_impact_pct напряму — старий код
        (`price_impact_pct > max_impact`) писався під V5, де знак був
        протилежний, і після переходу на V6 просто ніколи б не спрацював
        (реальний invalide price impact майже завжди від'ємний за цим
        визначенням, а від'ємне число не може бути "> 5.0").
        """
        max_impact = get_limit("MAX_PRICE_IMPACT_PCT")
        if -price_impact_pct > max_impact:
            return RiskCheckResult(
                allowed=False,
                reason=(
                    f"Price impact {price_impact_pct:.2f}% (невигідний бік) перевищує максимум "
                    f"{max_impact}%"
                ),
            )
        return RiskCheckResult(allowed=True)

    def check_confidence(self, confidence: float) -> RiskCheckResult:
        min_confidence = get_limit("MIN_SIGNAL_CONFIDENCE")
        if confidence < min_confidence:
            return RiskCheckResult(
                allowed=False,
                reason=(
                    f"Впевненість сигналу {confidence:.2f} нижче мінімуму "
                    f"{min_confidence}"
                ),
            )
        return RiskCheckResult(allowed=True)
