"""
П.3 аудиту: asyncio.to_thread для мережевих викликів у main.py (event loop
більше НЕ блокується на час запиту до OKX/DexScreener/Solana RPC) +
OPEN_POSITIONS_LOCK навколо "перевір MAX_OPEN_POSITIONS -> створи pending
buy-рядок", щоб два паралельні сигнали не могли обидва зайняти останній
вільний слот.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

import main as main_module
from core.config import settings
from core.storage import get_session, Trade
from core.okx_dex_client import QuoteResult, SwapResult


def _set_dry_run(value: bool):
    object.__setattr__(settings, "dry_run", value)


def _set_max_open_positions(value: int):
    object.__setattr__(settings, "max_open_positions", value)


def _install_common_mocks(monkeypatch, delay: float = 0.02):
    """Мокає всі мережеві залежності process_signal() — з невеликою затримкою
    (виконується в реальному thread pool через to_thread), щоб дати шанс
    ДРУГІЙ паралельній задачі теж просунутись до критичної секції."""
    monkeypatch.setattr(main_module.screener, "screen", lambda *a, **kw: (
        time.sleep(delay), SimpleNamespace(passed=True, liquidity_usd=100_000, age_hours=100, reasons_failed=[])
    )[1])
    monkeypatch.setattr(main_module, "get_wallet_balance", lambda: (
        time.sleep(delay), SimpleNamespace(usdt_balance=1000.0, sol_balance=1.0, is_real=True, low_gas_warning=False, note="")
    )[1])
    monkeypatch.setattr(main_module.dex, "get_quote", lambda *a, **kw: (
        time.sleep(delay), QuoteResult(success=True, from_amount="1000000", to_amount="500000", price_impact_pct=0.5)
    )[1])
    monkeypatch.setattr(main_module.dex, "execute_swap", lambda *a, **kw: (
        time.sleep(delay), SwapResult(success=True, tx_hash="TX", dry_run=True)
    )[1])
    monkeypatch.setattr(main_module, "fetch_price_usd", lambda *a, **kw: (time.sleep(delay), 0.001)[1])


def _fake_parsed(contract):
    return SimpleNamespace(
        is_signal=True, action="buy", token_symbol="COIN", contract_address=contract,
        chain="solana", confidence=0.9, reasoning="ok", raw_text=f"buy {contract}",
        amount_hint=None,
    )


async def test_parallel_buy_signals_race_on_last_slot(monkeypatch):
    """
    MAX_OPEN_POSITIONS=5, вже 4 зайнято (live), 1 вільний слот. Два сигнали
    на КУПІВЛЮ РІЗНИХ токенів запускаються паралельно — рівно один має
    зарезервувати слот (pending/confirmed), другий — отримати rejection
    через ліміт позицій. НЕ повинно бути 2 успішних buy чи 6 записів.
    """
    _set_dry_run(True)
    _set_max_open_positions(5)
    session = get_session()
    for i in range(4):
        session.add(Trade(action="buy", token_symbol=f"OLD{i}", contract_address=f"OLDC{i}",
                           chain="solana", dry_run=True, status="confirmed", amount_usd=5.0))
    session.commit()

    _install_common_mocks(monkeypatch, delay=0.03)
    monkeypatch.setattr(main_module, "notify", lambda client, text: asyncio.sleep(0))

    parsed_a = _fake_parsed("NewContractA")
    parsed_b = _fake_parsed("NewContractB")
    parse_map = {"signal A": parsed_a, "signal B": parsed_b}
    monkeypatch.setattr(main_module.parser, "parse", lambda text: parse_map[text])

    task_a = asyncio.create_task(main_module.process_signal(None, "signal A"))
    task_b = asyncio.create_task(main_module.process_signal(None, "signal B"))
    await asyncio.gather(task_a, task_b)

    session2 = get_session()
    new_trades = session2.query(Trade).filter(Trade.action == "buy", Trade.token_symbol == "COIN").all()
    assert len(new_trades) == 1, (
        f"рівно ОДИН з двох паралельних сигналів мав зарезервувати слот, отримано {len(new_trades)}"
    )
    assert new_trades[0].status in ("confirmed", "pending")

    all_buys = session2.query(Trade).filter(Trade.action == "buy").count()
    assert all_buys == 5, f"мало бути 4 старих + 1 новий = 5, отримано {all_buys}"


async def test_lock_released_after_exception_in_critical_section(monkeypatch):
    """
    Якщо перша задача впаде ВСЕРЕДИНІ критичної секції (після checkу ліміту,
    до звільнення lock) — друга задача не має зависнути назавжди, чекаючи lock.
    """
    _set_dry_run(True)
    _set_max_open_positions(5)
    _install_common_mocks(monkeypatch, delay=0.01)
    monkeypatch.setattr(main_module, "notify", lambda client, text: asyncio.sleep(0))

    call_count = {"n": 0}

    async def crashing_process_signal_wrapper():
        async with main_module.OPEN_POSITIONS_LOCK:
            call_count["n"] += 1
            raise RuntimeError("симуляція краху всередині критичної секції")

    with pytest.raises(RuntimeError):
        await crashing_process_signal_wrapper()

    # Друга задача, що бере той самий lock, має завершитись швидко (не зависнути)
    async def second():
        async with main_module.OPEN_POSITIONS_LOCK:
            call_count["n"] += 1

    await asyncio.wait_for(second(), timeout=1.0)
    assert call_count["n"] == 2


async def test_event_loop_stays_responsive_during_slow_signal(monkeypatch):
    """
    Поки process_signal() виконує "повільний" (замокано затримкою) мережевий
    виклик через asyncio.to_thread, event loop має лишатись вільним — інша
    паралельна корутина (аналог швидкого /status-запиту) завершується
    набагато швидше за загальну тривалість повільного сигналу.
    """
    _set_dry_run(True)
    _install_common_mocks(monkeypatch, delay=0.3)  # "повільний" мережевий виклик OKX
    monkeypatch.setattr(main_module, "notify", lambda client, text: asyncio.sleep(0))
    monkeypatch.setattr(main_module.parser, "parse", lambda text: _fake_parsed("SlowContract"))

    signal_task = asyncio.create_task(main_module.process_signal(None, "slow signal"))

    start = time.monotonic()
    await asyncio.sleep(0.01)  # даємо signal_task стартувати й дійти до to_thread
    # "швидкий /status" — проста синхронна DB-операція, як робить cmd_status()
    session = get_session()
    _ = session.query(Trade).count()
    fast_elapsed = time.monotonic() - start

    assert fast_elapsed < 0.15, (
        f"швидкий запит зайняв {fast_elapsed:.3f}с — event loop, схоже, заблокований повільним сигналом"
    )
    await signal_task


async def test_event_loop_stays_responsive_during_slow_claude_parse(monkeypatch):
    """
    П.2 (повторний аудит): parser.parse() тепер теж через asyncio.to_thread.
    Мокаємо ЛИШЕ parse() з затримкою (а не OKX-виклики) — event loop все одно
    має лишатись вільним саме на час розбору сигналу Claude API.
    """
    _set_dry_run(True)
    _install_common_mocks(monkeypatch, delay=0.0)  # інші виклики миттєві
    monkeypatch.setattr(main_module, "notify", lambda client, text: asyncio.sleep(0))

    def slow_parse(text):
        time.sleep(0.3)  # "повільний" виклик Claude API, виконується в thread pool
        return _fake_parsed("SlowClaudeContract")

    monkeypatch.setattr(main_module.parser, "parse", slow_parse)

    signal_task = asyncio.create_task(main_module.process_signal(None, "slow claude signal"))

    start = time.monotonic()
    await asyncio.sleep(0.01)
    session = get_session()
    _ = session.query(Trade).count()
    fast_elapsed = time.monotonic() - start

    assert fast_elapsed < 0.15, (
        f"швидкий запит зайняв {fast_elapsed:.3f}с — event loop заблокований повільним parser.parse()"
    )
    await signal_task
