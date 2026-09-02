"""
core/stats.py: PnL у відсотках у /статистика (від ПОТОЧНОГО балансу —
mock для dry-run, реальний для live), без ділення на нуль/None.
"""
import datetime as dt

import pytest

from core.stats import _compute_trade_stats, _format_trade_block, format_stats_report
from core.storage import get_session, Trade
from core.wallet import MOCK_WALLET_BALANCE_USD


def _far_past():
    return dt.datetime(2000, 1, 1)


def _make_closed_position(session, amount_usd, pnl_usd, dry_run=True):
    buy = Trade(action="buy", token_symbol="COIN", contract_address="C1", chain="solana",
                amount_usd=amount_usd, dry_run=dry_run, status="confirmed",
                entry_price=0.001, token_amount=1_000_000.0, triggered_levels="[]")
    session.add(buy)
    session.commit()
    sell = Trade(action="sell", token_symbol="COIN", contract_address="C1", chain="solana",
                 amount_usd=amount_usd + pnl_usd, pnl_usd=pnl_usd, token_amount=1_000_000.0,
                 dry_run=dry_run, status="confirmed", parent_trade_id=buy.id, close_reason="signal_sell")
    session.add(sell)
    session.commit()
    return buy, sell


def test_pnl_pct_computed_against_balance():
    """Баланс $100, PnL -$2 -> -2.0%."""
    session = get_session()
    _make_closed_position(session, amount_usd=10.0, pnl_usd=-2.0)

    t = _compute_trade_stats(session, _far_past(), dry_run=True, balance_usd=100.0)

    assert t.total_pnl_usd == pytest.approx(-2.0)
    assert t.total_pnl_pct == pytest.approx(-2.0)


def test_pnl_pct_none_when_balance_unavailable():
    """Баланс невідомий (None, напр. RPC недоступний) -> % не рахується, без падіння/inf/NaN."""
    session = get_session()
    _make_closed_position(session, amount_usd=10.0, pnl_usd=-2.0)

    t = _compute_trade_stats(session, _far_past(), dry_run=True, balance_usd=None)

    assert t.total_pnl_usd == pytest.approx(-2.0)
    assert t.total_pnl_pct is None


def test_pnl_pct_none_when_balance_zero():
    """Баланс 0 -> так само не ділимо (0 не є валідною базою для %), не inf/NaN."""
    session = get_session()
    _make_closed_position(session, amount_usd=10.0, pnl_usd=-2.0)

    t = _compute_trade_stats(session, _far_past(), dry_run=True, balance_usd=0.0)

    assert t.total_pnl_pct is None


def test_format_trade_block_shows_pct_when_available():
    session = get_session()
    _make_closed_position(session, amount_usd=10.0, pnl_usd=-2.0)
    t = _compute_trade_stats(session, _far_past(), dry_run=True, balance_usd=100.0)

    text = _format_trade_block("🧪 DRY RUN", t, balance_label="поточного mock-балансу драй-рану")

    assert "-2.0%" in text
    assert "поточного mock-балансу драй-рану" in text


def test_format_trade_block_shows_unavailable_note_when_no_balance():
    session = get_session()
    _make_closed_position(session, amount_usd=10.0, pnl_usd=-2.0)
    t = _compute_trade_stats(session, _far_past(), dry_run=True, balance_usd=None)

    text = _format_trade_block("🔴 LIVE", t, balance_label="поточного балансу гаманця")

    assert "% недоступний" in text
    assert "inf" not in text.lower() and "nan" not in text.lower()


def test_best_worst_trade_pct_unaffected_by_balance_change():
    """Найкраща/найгірша угода — % ВІД РОЗМІРУ ТІЄЇ угоди, не чіпається новою логікою."""
    session = get_session()
    _make_closed_position(session, amount_usd=10.0, pnl_usd=-2.0)
    t = _compute_trade_stats(session, _far_past(), dry_run=True, balance_usd=100.0)

    assert t.worst_trade is not None
    assert t.worst_trade[0] == pytest.approx(-20.0), "-2/10*100 = -20% від розміру УГОДИ, не від балансу $100"


def test_format_stats_report_uses_mock_balance_for_dry_run_regardless_of_live_balance():
    """DRY RUN секція завжди рахує % від MOCK_WALLET_BALANCE_USD, не від live_wallet_usdt_balance."""
    session = get_session()
    _make_closed_position(session, amount_usd=10.0, pnl_usd=-2.0, dry_run=True)

    report = format_stats_report(session, "day", live_wallet_usdt_balance=5.0)

    expected_pct = -2.0 / MOCK_WALLET_BALANCE_USD * 100
    assert f"{expected_pct:+.1f}%" in report


def test_format_stats_report_live_section_uses_passed_balance():
    session = get_session()
    _make_closed_position(session, amount_usd=10.0, pnl_usd=-2.0, dry_run=False)

    report = format_stats_report(session, "day", live_wallet_usdt_balance=100.0)

    assert "-2.0%" in report
    assert "поточного балансу гаманця" in report
