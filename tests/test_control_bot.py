"""
control_bot.py: owner/role-авторизація (IsAllowed/IsAdmin), і виключення
TEST_TOKEN_SYMBOL-записів з /balance, /positions, /history (Бага 1 сесії,
формалізовано тут разом з рештою тестового набору).
"""
from types import SimpleNamespace

import pytest

from core.config import settings
import core.runtime_state as runtime_state
from core.control_bot import IsAllowed, IsAdmin, get_role, _open_positions_total_usd, _force_close_position
from core.storage import get_session, Trade, TEST_TOKEN_SYMBOL
from core.position_monitor import remaining_amount, _position_locks, _divergence_block_counts


def _set_owner(user_id: int):
    object.__setattr__(settings, "tg_owner_user_id", user_id)


def _fake_event(user_id):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id))


# --- Авторизація ---

async def test_owner_is_always_admin():
    _set_owner(99999)
    assert get_role(99999) == "admin"


async def test_stranger_has_no_role():
    _set_owner(99999)
    assert get_role(12345) is None


async def test_is_allowed_filter_rejects_stranger():
    _set_owner(99999)
    is_allowed = IsAllowed()
    assert await is_allowed(_fake_event(12345)) is False


async def test_is_allowed_filter_accepts_added_user():
    _set_owner(99999)
    runtime_state.add_user(555, "user")
    is_allowed = IsAllowed()
    assert await is_allowed(_fake_event(555)) is True


async def test_is_admin_filter_rejects_readonly_user():
    """Доданий як 'user' (read-only) НЕ має пройти IsAdmin-фільтр (напр. /stop, /setlimit)."""
    _set_owner(99999)
    runtime_state.add_user(555, "user")
    is_admin = IsAdmin()
    assert await is_admin(_fake_event(555)) is False


async def test_is_admin_filter_accepts_added_admin():
    _set_owner(99999)
    runtime_state.add_user(777, "admin")
    is_admin = IsAdmin()
    assert await is_admin(_fake_event(777)) is True


async def test_is_admin_filter_accepts_owner():
    _set_owner(99999)
    is_admin = IsAdmin()
    assert await is_admin(_fake_event(99999)) is True


async def test_no_from_user_rejected():
    """Подія без from_user (напр. якийсь системний update) не повинна крашити фільтр."""
    _set_owner(99999)
    is_allowed = IsAllowed()
    event_without_user = SimpleNamespace()
    assert await is_allowed(event_without_user) is False


# --- Виключення TEST_TOKEN зі статистики (Бага 1) ---

def test_open_positions_total_usd_excludes_test_token():
    session = get_session()
    session.add(Trade(action="buy", token_symbol="REALCOIN", contract_address="C1",
                       chain="solana", amount_usd=5.0, dry_run=True, status="confirmed"))
    session.add(Trade(action="buy", token_symbol=TEST_TOKEN_SYMBOL, contract_address="TESTC",
                       chain="solana_test", amount_usd=100.0, dry_run=True, status="confirmed"))
    session.commit()

    total = _open_positions_total_usd(session)
    assert total == pytest.approx(5.0), f"тестова позиція $100 не мала потрапити в підсумок, отримано {total}"


# --- Примусове закриття позиції ---

def _make_open_buy(session, amount_usd=100.0, token_amount=1_000_000.0):
    buy = Trade(
        action="buy", token_symbol="RUGCOIN", contract_address="RUGC1",
        chain="solana", amount_usd=amount_usd, dry_run=True, status="confirmed",
        entry_price=0.0001, token_amount=token_amount, triggered_levels="[]",
    )
    session.add(buy)
    session.commit()
    return buy


async def test_force_close_zeroes_remaining_and_records_full_loss():
    session = get_session()
    buy = _make_open_buy(session, amount_usd=100.0, token_amount=1_000_000.0)
    assert remaining_amount(session, buy) == pytest.approx(1_000_000.0)

    success, msg = await _force_close_position(buy.id)

    assert success is True
    assert "примусово закрито" in msg
    assert "$100.00" in msg

    session2 = get_session()
    buy2 = session2.get(Trade, buy.id)
    assert remaining_amount(session2, buy2) == pytest.approx(0.0, abs=1e-6)

    sells = session2.query(Trade).filter(Trade.parent_trade_id == buy.id).all()
    assert len(sells) == 1
    assert sells[0].close_reason == "manual_force_close"
    assert sells[0].status == "confirmed"
    assert sells[0].amount_usd == 0.0
    assert sells[0].pnl_usd == pytest.approx(-100.0), "весь залишок мав зарахуватись як повний збиток, не 0 і не прибуток"


async def test_force_close_excluded_from_open_positions():
    from core.position_monitor import _get_open_positions

    session = get_session()
    buy = _make_open_buy(session)
    assert len(_get_open_positions(session)) == 1

    await _force_close_position(buy.id)

    session2 = get_session()
    assert _get_open_positions(session2) == [], "закрита позиція не має більше з'являтись у ladder-моніторингу"


async def test_force_close_clears_lock_and_divergence_counter():
    session = get_session()
    buy = _make_open_buy(session)
    _position_locks[buy.id] = __import__("asyncio").Lock()
    _divergence_block_counts[buy.id] = 3

    await _force_close_position(buy.id)

    assert buy.id not in _position_locks
    assert buy.id not in _divergence_block_counts


async def test_force_close_already_closed_position_fails_gracefully():
    session = get_session()
    buy = _make_open_buy(session, amount_usd=50.0, token_amount=500_000.0)
    success1, _ = await _force_close_position(buy.id)
    assert success1 is True

    success2, msg2 = await _force_close_position(buy.id)
    assert success2 is False
    assert "вже закрита" in msg2


async def test_force_close_nonexistent_buy_id_fails_gracefully():
    success, msg = await _force_close_position(999_999)
    assert success is False
    assert "не знайдено" in msg


async def test_force_close_stats_show_loss_not_profit():
    from core.stats import _compute_trade_stats

    session = get_session()
    buy = _make_open_buy(session, amount_usd=75.0, token_amount=1_000_000.0)
    await _force_close_position(buy.id)

    session2 = get_session()
    t = _compute_trade_stats(session2, __import__("datetime").datetime(2000, 1, 1), dry_run=True)
    assert t.total_pnl_usd is not None
    assert t.total_pnl_usd == pytest.approx(-75.0), "статистика має показати повний збиток, не пропустити і не показати прибуток"
    assert t.winning_sell_count == 0
