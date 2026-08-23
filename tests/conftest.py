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
