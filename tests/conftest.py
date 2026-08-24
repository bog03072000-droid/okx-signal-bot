"""
Спільні fixtures для всього тестового набору.

fresh_db (autouse) — перед КОЖНИМ тестом пересвʼязує core.storage.engine/
SessionLocal на нову in-memory SQLite базу і створює таблиці. get_session()
(core/storage.py) читає SessionLocal як module-level ім'я в МОМЕНТ виклику
(не на момент імпорту) — тому це пересвʼязування діє для БУДЬ-ЯКОГО коду,
що викликає get_session() (risk_manager, position_monitor, control_bot,
stats, main), без потреби мокати кожен виклик окремо. Ізолює тести один
від одного і від реальної data/bot.db.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import core.storage as storage
import core.runtime_state as runtime_state
import core.risk_manager as risk_manager
from core.config import settings


@pytest.fixture(autouse=True)
def isolated_cooldown_state():
    """
    RiskManager._last_trade_time (core/risk_manager.py) — module-level dict,
    СПІЛЬНИЙ для БУДЬ-ЯКОЇ кількості RiskManager()-інстансів (навмисно, щоб
    один спільний cooldown діяв незалежно від того, скільки окремих
    RiskManager створено — див. docstring self_test.py). Той самий "плюс"
    у проді стає джерелом протікання стану МІЖ тестами: якщо один тест
    зареєстрував trade_time для "solana", наступний тест (інший файл,
    інший порядок запуску) міг би отримати check_cooldown()=False там, де
    очікував True.
    """
    risk_manager._last_trade_time.clear()
    yield
    risk_manager._last_trade_time.clear()


@pytest.fixture(autouse=True)
def isolated_settings():
    """
    settings — frozen dataclass singleton (core/config.py); тести мутують
    його поля напряму через object.__setattr__ (dry_run, max_open_positions,
    daily_loss_limit_pct, solana_private_key тощо), бо звичайний setattr()
    заблокований. Без цієї fixture зміна в ОДНОМУ тестовому файлі "витікає"
    в наступні (виявлено емпірично: test_self_test.py падав лише в
    ПОВНОМУ прогоні pytest, але проходив ізольовано — через
    settings.dry_run/daily_loss_limit_pct, залишені іншим тестом). Знімок
    ВСІХ полів перед тестом і відновлення після — простіше й надійніше, ніж
    вручну перераховувати, які саме поля хтось десь може змінити.
    """
    snapshot = dict(vars(settings))
    yield
    for field_name, value in snapshot.items():
        object.__setattr__(settings, field_name, value)


@pytest.fixture(autouse=True)
def fresh_db():
    storage.engine = storage.create_engine("sqlite:///:memory:", echo=False)
    storage.SessionLocal = storage.sessionmaker(bind=storage.engine)
    storage.Base.metadata.create_all(storage.engine)
    yield


@pytest.fixture(autouse=True)
def isolated_runtime_state(tmp_path):
    """
    runtime_state.STATE_FILE (data/runtime_state.json) МІГ БИ мати реальні
    /setlimit-перевизначення з локального диска — без цієї ізоляції тест,
    що виставляє settings.max_open_positions напряму, міг би мовчки
    ігноруватись, бо core/config.py:get_limit() спершу перевіряє
    runtime_state override. Перенаправляємо на тимчасовий файл на весь тест.
    """
    runtime_state.STATE_FILE = str(tmp_path / "runtime_state_test.json")
    yield
