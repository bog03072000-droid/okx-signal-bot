"""
control_bot.py: owner/role-авторизація (IsAllowed/IsAdmin), і виключення
TEST_TOKEN_SYMBOL-записів з /balance, /positions, /history (Бага 1 сесії,
формалізовано тут разом з рештою тестового набору).
"""
from types import SimpleNamespace

import pytest

from core.config import settings
import core.runtime_state as runtime_state
from core.control_bot import IsAllowed, IsAdmin, get_role, _open_positions_total_usd
from core.storage import get_session, Trade, TEST_TOKEN_SYMBOL


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
