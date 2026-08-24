"""
Кнопка "🧪 Тест" (core/self_test.py): переконуємось, що ізоляція від реальних
угод і реальної статистики й досі коректна після всіх змін цієї й попередньої
сесій — force_dry_run завжди True, TEST_TOKEN_SYMBOL завжди проставлений,
chain="solana_test" (не "solana"), прибирання відбувається навіть при винятку.
"""
import pytest

import core.self_test as st
import core.position_monitor as pm
from core.storage import get_session, Trade, TEST_TOKEN_SYMBOL
from core.okx_dex_client import QuoteResult, SwapResult


@pytest.fixture(autouse=True)
def _fast_test(monkeypatch):
    # QUOTE_CALL_DELAY_SECONDS=1.0 в реальному коді — навмисно, щоб не
    # впертись в rate limit OKX (див. коментар в self_test.py). У тестах
    # це лише сповільнює прогін без жодної користі — прибираємо паузу.
    monkeypatch.setattr(st, "QUOTE_CALL_DELAY_SECONDS", 0.0)


def _mock_quote(*a, **kw):
    return QuoteResult(success=True, from_amount="1000000", to_amount="700000", price_impact_pct=1.0)


def _patch_both_dex_clients(monkeypatch, get_quote=None, execute_swap=None):
    """
    run_ladder_test() використовує ДВА різних OKXDexClient-інстанси:
    self_test._dex — лише для власної діагностичної quote()-перевірки перед
    сценаріями; core.position_monitor.dex — той, що РЕАЛЬНО викликається
    зсередини _check_position()/execute_partial_sell() на кожному рівні
    (self_test навмисно імпортує ladder-логіку напряму з position_monitor.py,
    а не копіює її — див. docstring self_test.py). Мокати треба ОБИДВА,
    інакше рівні SL/TP тихо б'ються об РЕАЛЬНИЙ OKX API.
    """
    gq = get_quote or _mock_quote
    es = execute_swap or (lambda *a, **kw: SwapResult(success=True, tx_hash="SIM", dry_run=True))
    monkeypatch.setattr(st._dex, "get_quote", gq)
    monkeypatch.setattr(st._dex, "execute_swap", es)
    monkeypatch.setattr(pm.dex, "get_quote", gq)
    monkeypatch.setattr(pm.dex, "execute_swap", es)


async def test_ladder_test_always_forces_dry_run(monkeypatch):
    """execute_swap() МАЄ отримати force_dry_run=True на КОЖНОМУ виклику всередині тесту."""
    swap_calls = []

    def capturing_execute_swap(*args, **kwargs):
        swap_calls.append(kwargs.get("force_dry_run"))
        return SwapResult(success=True, tx_hash="SIM", dry_run=True)

    _patch_both_dex_clients(monkeypatch, execute_swap=capturing_execute_swap)

    await st.run_ladder_test()

    assert len(swap_calls) > 0, "тест мав викликати execute_swap хоча б раз (SL/TP рівні спрацювали)"
    assert all(v is True for v in swap_calls), (
        f"force_dry_run мав бути True на КОЖНОМУ виклику, отримано: {swap_calls}"
    )


def _spy_on_added_trades(monkeypatch):
    """
    Перехоплює Trade-об'єкти, які run_ladder_test() додає у СВОЮ сесію
    (self_test.py імпортує get_session за іменем — патчимо саме цю назву
    в модулі self_test), ДО того, як finally-блок їх прибере. Повертає
    список [(token_symbol, chain, action), ...] у порядку додавання.
    """
    captured = []
    real_get_session = st.get_session

    def spy_get_session():
        session = real_get_session()
        orig_add = session.add

        def spy_add(obj):
            if isinstance(obj, Trade):
                captured.append((obj.token_symbol, obj.chain, obj.action))
            return orig_add(obj)

        session.add = spy_add
        return session

    monkeypatch.setattr(st, "get_session", spy_get_session)
    return captured


async def test_ladder_test_uses_test_token_symbol_and_test_chain(monkeypatch):
    """Тимчасові позиції мають token_symbol=TEST_TOKEN_SYMBOL і chain='solana_test' (ізоляція від прод-моніторингу)."""
    captured = _spy_on_added_trades(monkeypatch)
    _patch_both_dex_clients(monkeypatch)

    await st.run_ladder_test()

    buy_rows = [c for c in captured if c[2] == "buy"]
    assert len(buy_rows) >= 2, f"мало бути щонайменше 2 buy-рядки (SL і TP сценарії): {captured}"
    for token_symbol, chain, action in captured:
        assert token_symbol == TEST_TOKEN_SYMBOL, f"token_symbol={token_symbol!r}, очікувалось {TEST_TOKEN_SYMBOL!r}"
        assert chain == "solana_test", (
            f"chain={chain!r} — МАЄ бути 'solana_test', інакше реальний position_monitor_loop() "
            f"підхопить цю тестову позицію (chain=='solana' фільтр в _get_open_positions())"
        )


async def test_ladder_test_cleans_up_after_success(monkeypatch):
    _patch_both_dex_clients(monkeypatch)

    await st.run_ladder_test()

    session = get_session()
    leftover = session.query(Trade).filter(Trade.token_symbol == TEST_TOKEN_SYMBOL).count()
    assert leftover == 0, f"після тесту лишилось {leftover} тестових рядків — прибирання не спрацювало"


async def test_ladder_test_cleans_up_even_on_exception(monkeypatch):
    """Виняток посеред сценарію (напр. quote впав після діагностики) — тестові рядки все одно прибираються (finally)."""
    call_count = {"n": 0}

    def flaky_quote(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # діагностика проходить
            return QuoteResult(success=True, from_amount="1000000", to_amount="700000", price_impact_pct=1.0)
        raise RuntimeError("симуляція несподіваного краху всередині сценарію")

    _patch_both_dex_clients(monkeypatch, get_quote=flaky_quote)

    with pytest.raises(RuntimeError):
        await st.run_ladder_test()

    session = get_session()
    leftover = session.query(Trade).filter(Trade.token_symbol == TEST_TOKEN_SYMBOL).count()
    assert leftover == 0, f"навіть при винятку прибирання (finally) мало спрацювати, лишилось {leftover}"


async def test_ladder_test_diagnostic_failure_short_circuits(monkeypatch):
    """Якщо ДІАГНОСТИЧНИЙ quote() не проходить — звіт зупиняється одразу, не показує 5x 'баг у ladder-логіці'."""
    monkeypatch.setattr(st._dex, "get_quote", lambda *a, **kw: QuoteResult(success=False, error="401 Unauthorized"))

    report = await st.run_ladder_test()
    text = "\n".join(report)

    assert "Діагностика" in text
    assert "не баг у ladder-логіці" in text.lower() or "а не баг" in text
    # Короткий early-exit звіт (заголовок + порожній рядок + fail + пояснення),
    # а НЕ 5 незрозумілих "рівень не спрацював" по кожному ladder-рівню.
    assert len(report) <= 4, f"звіт задовгий для early-exit сценарію: {report}"


async def test_buy_signal_test_never_writes_to_real_tables(monkeypatch):
    """run_buy_signal_test() не має писати жодних SignalLog/Trade рядків у реальні таблиці."""
    from core.storage import SignalLog

    def mock_parse(text):
        from types import SimpleNamespace
        return SimpleNamespace(
            is_signal=True, action="buy", token_symbol=None,
            contract_address="JACCJHVy2QC96VNJK1iMrqYwMQPBbHNna2oEnxEPpump",
            chain="solana", confidence=0.8, reasoning="ok", raw_text=st.TEST_SIGNAL_TEXT, amount_hint=None,
        )
    monkeypatch.setattr(st._parser, "parse", mock_parse)
    monkeypatch.setattr(st._screener, "screen", lambda *a, **kw: __import__("types").SimpleNamespace(
        passed=True, liquidity_usd=100_000, age_hours=100, reasons_failed=[]
    ))
    monkeypatch.setattr(st, "get_wallet_balance", lambda: __import__("types").SimpleNamespace(
        usdt_balance=1000.0, sol_balance=1.0, is_real=True, low_gas_warning=False, note=""
    ))
    swap_calls = []
    monkeypatch.setattr(st._dex, "get_quote", _mock_quote)
    monkeypatch.setattr(st._dex, "execute_swap", lambda *a, **kw: (swap_calls.append(kw.get("force_dry_run")), SwapResult(success=True, tx_hash="SIM", dry_run=True))[1])

    session = get_session()
    trades_before = session.query(Trade).count()
    logs_before = session.query(SignalLog).count()

    await st.run_buy_signal_test()

    trades_after = session.query(Trade).count()
    logs_after = session.query(SignalLog).count()

    assert trades_after == trades_before, "run_buy_signal_test() не мав писати в Trade"
    assert logs_after == logs_before, "run_buy_signal_test() не мав писати в SignalLog"
    assert swap_calls == [True], "execute_swap мав отримати force_dry_run=True"
