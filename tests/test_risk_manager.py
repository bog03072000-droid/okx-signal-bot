"""
П.1 аудиту: check_daily_loss_limit() і check_open_positions_limit() раніше
рахували PnL/позиції БЕЗ розрізнення dry_run/live і без виключення тестових
записів від кнопки "🧪 Тест" (token_symbol=TEST_TOKEN_SYMBOL) — фейковий
PnL/позиції могли або хибно зупинити live-торгівлю, або замаскувати
реальний живий збиток. Тести нижче підтверджують фікс на конкретних
edge cases з постановки задачі.
"""
import datetime as dt

from core.config import settings
from core.storage import get_session, Trade, TEST_TOKEN_SYMBOL
from core.risk_manager import RiskManager


def _set_dry_run(value: bool):
    # settings — frozen dataclass (core/config.py), звичайний setattr()
    # заблокований навмисно (щоб конфігурація не мінялась випадково десь
    # посеред коду) — object.__setattr__ обходить це ТІЛЬКИ в тестах, і
    # мутує єдиний спільний singleton-інстанс, тому зміна видна одразу й
    # у risk_manager.py, і будь-де ще, що тримає посилання на той самий
    # об'єкт settings.
    object.__setattr__(settings, "dry_run", value)


def test_daily_loss_limit_ignores_old_dry_run_loss_in_live_mode():
    """Edge case 1: live-режим, стара dry-run угода -50 + реальна live -2 -> враховує лише -2."""
    _set_dry_run(False)
    session = get_session()
    session.add(Trade(action="sell", token_symbol="OLDCOIN", contract_address="C1",
                       chain="solana", dry_run=True, status="confirmed", pnl_usd=-50.0))
    session.add(Trade(action="sell", token_symbol="REALCOIN", contract_address="C2",
                       chain="solana", dry_run=False, status="confirmed", pnl_usd=-2.0))
    session.commit()

    risk = RiskManager()
    # ліміт достатньо великий, щоб -2 не спрацював, а -52 спрацював би —
    # це і є перевірка "рахуємо лише -2, а не -52"
    result_should_pass = risk.check_daily_loss_limit(wallet_balance_usd=100.0)
    assert result_should_pass.allowed, (
        f"мало пройти (-2 USD з $100 << ліміт), але: {result_should_pass.reason}"
    )


def test_daily_loss_limit_excludes_test_button_pnl():
    """Edge case 2: тестова угода +10 + реальна live -8 -> сумарний PnL має бути -8, не +2."""
    _set_dry_run(False)
    session = get_session()
    session.add(Trade(action="sell", token_symbol=TEST_TOKEN_SYMBOL, contract_address="TESTC",
                       chain="solana_test", dry_run=True, status="confirmed", pnl_usd=10.0))
    session.add(Trade(action="sell", token_symbol="REALCOIN", contract_address="C2",
                       chain="solana", dry_run=False, status="confirmed", pnl_usd=-8.0))
    session.commit()

    risk = RiskManager()
    # Ліміт 5% від $100 = -$5. Реальний PnL -8 перевищує це (-8% > 5%) —
    # має ВІДХИЛИТИ. Якби тестовий +10 домішався (сума = +2), воно б
    # хибно ПРОЙШЛО (+2 не збиток) — це і є перевірка ізоляції.
    object.__setattr__(settings, "daily_loss_limit_pct", 5.0)
    result = risk.check_daily_loss_limit(wallet_balance_usd=100.0)
    assert not result.allowed, (
        "мало відхилити: реальний збиток -8 USD (-8% від $100) перевищує ліміт 5%, "
        "тестовий +10 не має це маскувати"
    )


def test_open_positions_limit_excludes_old_dry_run_positions():
    """Edge case 3: 3 старі dry-run buy + 2 live buy, MAX_OPEN_POSITIONS=5 в live -> '2 зайнято з 5'."""
    _set_dry_run(False)
    session = get_session()
    for i in range(3):
        session.add(Trade(action="buy", token_symbol=f"OLD{i}", contract_address=f"OLDC{i}",
                           chain="solana", dry_run=True, status="confirmed", amount_usd=5.0))
    for i in range(2):
        session.add(Trade(action="buy", token_symbol=f"LIVE{i}", contract_address=f"LIVEC{i}",
                           chain="solana", dry_run=False, status="confirmed", amount_usd=5.0))
    session.commit()

    object.__setattr__(settings, "max_open_positions", 5)
    risk = RiskManager()
    result = risk.check_open_positions_limit()
    assert result.allowed, (
        f"мало пройти: лише 2 live-позиції зайнято з 5 (3 старих dry-run не рахуються), "
        f"отримав: {result.reason}"
    )

    # Додаємо ще 3 live buy (разом 5 live) -> ліміт МАЄ спрацювати
    for i in range(2, 5):
        session.add(Trade(action="buy", token_symbol=f"LIVE{i}", contract_address=f"LIVEC{i}",
                           chain="solana", dry_run=False, status="confirmed", amount_usd=5.0))
    session.commit()
    result_full = risk.check_open_positions_limit()
    assert not result_full.allowed, "5 live-позицій з лімітом 5 має відхилити"


def test_dry_run_mode_still_counts_dry_run_trades():
    """Edge case 4: dry-run режим і далі коректно рахує dry-run угоди (протилежний напрямок фільтра)."""
    _set_dry_run(True)
    session = get_session()
    session.add(Trade(action="sell", token_symbol="DRYCOIN", contract_address="D1",
                       chain="solana", dry_run=True, status="confirmed", pnl_usd=-50.0))
    # Ця "жива" угода не має жодного стосунку до dry-run режиму — не рахується
    session.add(Trade(action="sell", token_symbol="LIVECOIN", contract_address="D2",
                       chain="solana", dry_run=False, status="confirmed", pnl_usd=+1000.0))
    session.commit()

    object.__setattr__(settings, "daily_loss_limit_pct", 10.0)
    risk = RiskManager()
    result = risk.check_daily_loss_limit(wallet_balance_usd=100.0)
    assert not result.allowed, (
        "у dry-run режимі -50 USD (-50% від $100) з dry-run угоди МАЄ врахуватись "
        "і перевищити ліміт 10% — незалежно від того, що є +1000 live"
    )


def test_open_positions_limit_test_token_excluded_even_if_dry_run_matches():
    """
    TEST_TOKEN_SYMBOL рядки мають dry_run=True (self_test форсує це завжди) —
    навіть якщо бот сам зараз у dry-run режимі, тестові рядки НЕ мають
    рахуватись як реальні dry-run позиції.
    """
    _set_dry_run(True)
    session = get_session()
    session.add(Trade(action="buy", token_symbol=TEST_TOKEN_SYMBOL, contract_address="TESTC",
                       chain="solana_test", dry_run=True, status="confirmed", amount_usd=10.0))
    session.commit()

    object.__setattr__(settings, "max_open_positions", 5)
    risk = RiskManager()
    result = risk.check_open_positions_limit()
    assert result.allowed
