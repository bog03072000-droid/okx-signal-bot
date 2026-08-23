"""
Ladder TP/SL: послідовні рівні, epsilon на межі порогу, price-divergence
guard (MAX_PRICE_DIVERGENCE_PCT), і pending/failed edge cases П.2 для
execute_partial_sell().
"""
import pytest

import core.position_monitor as pm
from core.storage import get_session, Trade
from core.okx_dex_client import QuoteResult, SwapResult


def _make_buy(session, entry_price=1.243e-05, amount_usd=0.078, token_amount=1_000_000.0,
              triggered_levels="[]"):
    buy = Trade(
        action="buy", token_symbol="REALCOIN", contract_address="RealContractAddr1234",
        chain="solana", amount_usd=amount_usd, dry_run=True, status="confirmed",
        entry_price=entry_price, token_amount=token_amount, triggered_levels=triggered_levels,
    )
    session.add(buy)
    session.commit()
    return buy


def _consistent_quote_factory(cost_per_raw_unit):
    """Quote-мок, де ціна котирування рахується КОНСИСТЕНТНО з pct-параметром виклику."""
    def make(pct):
        def _quote(from_token, to_token, amount_raw, chain_id="501"):
            raw = float(amount_raw)
            price_per_raw_unit = cost_per_raw_unit * (1 + pct)
            to_amount = raw * price_per_raw_unit * (10 ** 6)
            return QuoteResult(success=True, from_amount=amount_raw, to_amount=str(int(to_amount)), price_impact_pct=0.0)
        return _quote
    return make


@pytest.fixture(autouse=True)
def _notify_noop(monkeypatch):
    async def noop(text):
        pass
    monkeypatch.setattr(pm, "notify_owner", noop)


@pytest.fixture(autouse=True)
def _no_real_swap(monkeypatch):
    def mock_execute_swap(*a, **kw):
        return SwapResult(success=True, tx_hash="MOCK_TX", dry_run=True)
    monkeypatch.setattr(pm.dex, "execute_swap", mock_execute_swap)


# --- Ladder SL/TP: послідовні рівні, epsilon ---

async def test_sl_10_then_20_sequential_levels():
    session = get_session()
    buy = _make_buy(session)
    cost_per_raw_unit = buy.amount_usd / buy.token_amount
    quote_factory = _consistent_quote_factory(cost_per_raw_unit)

    pm.dex.get_quote = quote_factory(-0.10)
    await pm._check_position(session, buy, buy.entry_price * 0.90, force_dry_run=False)
    assert "stop_loss_-10pct" in pm._triggered_levels(buy)
    assert pm.remaining_amount(session, buy) == pytest.approx(500_000.0)

    pm.dex.get_quote = quote_factory(-0.20)
    await pm._check_position(session, buy, buy.entry_price * 0.80, force_dry_run=False)
    assert "stop_loss_-20pct" in pm._triggered_levels(buy)
    assert pm.remaining_amount(session, buy) == pytest.approx(0.0, abs=1e-6)


async def test_pct_epsilon_triggers_at_exact_threshold():
    """Float-округлення на РІВНО -10% (напр. -9.999999999999994%) не має пропускати рівень."""
    session = get_session()
    buy = _make_buy(session, entry_price=0.0000001234)
    cost_per_raw_unit = buy.amount_usd / buy.token_amount
    pm.dex.get_quote = _consistent_quote_factory(cost_per_raw_unit)(-0.10)

    price_exact = buy.entry_price * 0.90  # у float може дати -9.999999999999994%
    await pm._check_position(session, buy, price_exact, force_dry_run=False)
    assert "stop_loss_-10pct" in pm._triggered_levels(buy)


async def test_tp_levels_sequential():
    session = get_session()
    buy = _make_buy(session)
    cost_per_raw_unit = buy.amount_usd / buy.token_amount
    quote_factory = _consistent_quote_factory(cost_per_raw_unit)

    for pct, level in [(0.30, "take_profit_+30pct"), (0.60, "take_profit_+60pct"), (1.00, "take_profit_+100pct")]:
        pm.dex.get_quote = quote_factory(pct)
        await pm._check_position(session, buy, buy.entry_price * (1 + pct), force_dry_run=False)
        assert level in pm._triggered_levels(buy)
    assert pm.remaining_amount(session, buy) == pytest.approx(0.0, abs=1e-6)


# --- Price divergence guard ---

async def test_divergence_over_limit_blocks_swap_and_notifies(monkeypatch):
    session = get_session()
    buy = _make_buy(session, triggered_levels='["stop_loss_-10pct"]')
    cost_per_raw_unit = buy.amount_usd / buy.token_amount

    notifications = []
    async def capture(text):
        notifications.append(text)
    monkeypatch.setattr(pm, "notify_owner", capture)

    swap_calls = []
    monkeypatch.setattr(pm.dex, "execute_swap", lambda *a, **kw: swap_calls.append(1) or SwapResult(success=True, tx_hash="X", dry_run=True))

    def mock_quote(from_token, to_token, amount_raw, chain_id="501"):
        raw = float(amount_raw)
        # OKX реально показує -13%, DexScreener тригерить -20% -> розбіжність >5%
        price = cost_per_raw_unit * 0.87
        return QuoteResult(success=True, from_amount=amount_raw, to_amount=str(int(raw * price * 10**6)), price_impact_pct=0.0)
    pm.dex.get_quote = mock_quote

    await pm._check_position(session, buy, buy.entry_price * 0.80, force_dry_run=False)

    assert len(swap_calls) == 0, "своп НЕ мав виконатись при розбіжності понад ліміт"
    assert "stop_loss_-20pct" not in pm._triggered_levels(buy)
    assert len(notifications) == 1
    assert "Розбіжність цін" in notifications[0]
    sells = session.query(Trade).filter(Trade.parent_trade_id == buy.id).all()
    assert len(sells) == 0


async def test_divergence_within_limit_executes_normally():
    session = get_session()
    buy = _make_buy(session, triggered_levels='["stop_loss_-10pct"]')
    cost_per_raw_unit = buy.amount_usd / buy.token_amount

    def mock_quote(from_token, to_token, amount_raw, chain_id="501"):
        raw = float(amount_raw)
        price = cost_per_raw_unit * 0.824  # ~3% розбіжність від очікуваних 0.80 (-20%)
        return QuoteResult(success=True, from_amount=amount_raw, to_amount=str(int(raw * price * 10**6)), price_impact_pct=0.0)
    pm.dex.get_quote = mock_quote

    await pm._check_position(session, buy, buy.entry_price * 0.80, force_dry_run=False)

    assert "stop_loss_-20pct" in pm._triggered_levels(buy)
    sells = session.query(Trade).filter(Trade.parent_trade_id == buy.id).all()
    assert len(sells) == 1
    assert sells[0].status == "confirmed"


async def test_divergence_check_skipped_for_force_dry_run(monkeypatch):
    """
    force_dry_run=True (кнопка "🧪 Тест") використовує синтетичні числа проти
    РЕАЛЬНОГО OKX quote — розбіжність завжди величезна, але перевірку НЕ
    застосовуємо тут (немає реального свопу, який захищати).
    """
    session = get_session()
    buy = _make_buy(session, entry_price=0.001, amount_usd=10.0, token_amount=1_000_000.0)

    def huge_divergence_quote(from_token, to_token, amount_raw, chain_id="501"):
        # Реальна ціна на кілька порядків більша за очікувану (як реальний Wrapped SOL)
        raw = float(amount_raw)
        return QuoteResult(success=True, from_amount=amount_raw, to_amount=str(int(raw * 190 * 10**6)), price_impact_pct=0.0)
    monkeypatch.setattr(pm.dex, "get_quote", huge_divergence_quote)

    await pm._check_position(session, buy, buy.entry_price * 0.90, force_dry_run=True)
    assert "stop_loss_-10pct" in pm._triggered_levels(buy), (
        "force_dry_run має обходити divergence-перевірку — інакше кнопка Тест завжди блокувалась б"
    )


# --- П.2: pending/failed edge cases для execute_partial_sell() ---

async def test_pending_row_created_before_swap_survives_crash(monkeypatch):
    """
    Симулюємо виняток ОДРАЗУ після успішного quote(), до execute_swap() —
    pending-рядок, створений ДО цього винятку, має лишитись у БД зі
    status="pending" (а не бути відсутнім взагалі).
    """
    session = get_session()
    buy = _make_buy(session)

    def mock_quote(*a, **kw):
        return QuoteResult(success=True, from_amount="500000", to_amount="35100", price_impact_pct=0.0)
    monkeypatch.setattr(pm.dex, "get_quote", mock_quote)

    def crash(*a, **kw):
        raise RuntimeError("симуляція краху процесу під час відправки транзакції")
    monkeypatch.setattr(pm.dex, "execute_swap", crash)

    with pytest.raises(RuntimeError):
        await pm.execute_partial_sell(session, buy, "stop_loss_-10pct", 500_000.0, buy.entry_price * 0.90, force_dry_run=False)

    sells = session.query(Trade).filter(Trade.parent_trade_id == buy.id).all()
    assert len(sells) == 1, "pending-рядок мав бути записаний ДО execute_swap()"
    assert sells[0].status == "pending"
    assert sells[0].tx_hash is None


async def test_successful_sell_updates_same_pending_row_not_duplicate():
    session = get_session()
    buy = _make_buy(session)

    def mock_quote(*a, **kw):
        return QuoteResult(success=True, from_amount="500000", to_amount="35100", price_impact_pct=0.0)
    pm.dex.get_quote = mock_quote
    pm.dex.execute_swap = lambda *a, **kw: SwapResult(success=True, tx_hash="REAL_TX", dry_run=True)

    result = await pm.execute_partial_sell(session, buy, "stop_loss_-10pct", 500_000.0, buy.entry_price * 0.90, force_dry_run=False)
    assert result is True

    sells = session.query(Trade).filter(Trade.parent_trade_id == buy.id).all()
    assert len(sells) == 1, "має бути ОДИН рядок (оновлений pending), не дублікат"
    assert sells[0].status == "confirmed"
    assert sells[0].tx_hash == "REAL_TX"


async def test_failed_swap_marks_failed_and_position_stays_open():
    session = get_session()
    buy = _make_buy(session)

    def mock_quote(*a, **kw):
        return QuoteResult(success=True, from_amount="500000", to_amount="35100", price_impact_pct=0.0)
    pm.dex.get_quote = mock_quote
    pm.dex.execute_swap = lambda *a, **kw: SwapResult(success=False, dry_run=True, error="quote застарів")

    remaining_before = pm.remaining_amount(session, buy)
    result = await pm.execute_partial_sell(session, buy, "stop_loss_-10pct", 500_000.0, buy.entry_price * 0.90, force_dry_run=False)
    assert result is False

    sells = session.query(Trade).filter(Trade.parent_trade_id == buy.id).all()
    assert len(sells) == 1
    assert sells[0].status == "failed"
    assert sells[0].failure_reason == "quote застарів"
    assert "stop_loss_-10pct" not in pm._triggered_levels(buy), "невдалий своп не має позначати рівень"

    remaining_after = pm.remaining_amount(session, buy)
    assert remaining_after == remaining_before, (
        "невдалий (failed) sell НЕ має зменшувати remaining_amount — позиція лишається такою ж відкритою"
    )
