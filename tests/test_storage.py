"""
core/storage.py: Trade.status="pending"/failure_reason (П.2), і що
_migrate_trades_table() коректно додає нові колонки (включно з
failure_reason, доданим у цьому кроці) до "старої" таблиці, яка існувала
до їх появи.
"""
from sqlalchemy import inspect, text

import core.storage as storage
from core.storage import get_session, Trade


def test_pending_status_and_failure_reason_roundtrip():
    session = get_session()
    trade = Trade(action="buy", token_symbol="X", contract_address="C1", chain="solana",
                   amount_usd=5.0, dry_run=True, status="pending")
    session.add(trade)
    session.commit()
    trade_id = trade.id

    session2 = get_session()
    fetched = session2.get(Trade, trade_id)
    assert fetched.status == "pending"
    assert fetched.tx_hash is None
    assert fetched.failure_reason is None

    fetched.status = "failed"
    fetched.failure_reason = "quote застарів"
    session2.commit()

    session3 = get_session()
    fetched2 = session3.get(Trade, trade_id)
    assert fetched2.status == "failed"
    assert fetched2.failure_reason == "quote застарів"


def test_migration_adds_new_columns_to_old_table():
    """
    Симулює data/bot.db зі СТАРОЮ схемою (до появи ladder-полів і
    failure_reason) — _migrate_trades_table() має додати всі відсутні
    колонки без падіння "no such column".
    """
    engine = storage.create_engine("sqlite:///:memory:", echo=False)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY, created_at DATETIME, action VARCHAR, "
            "token_symbol VARCHAR, contract_address VARCHAR, chain VARCHAR, "
            "amount_usd FLOAT, price FLOAT, tx_hash VARCHAR, dry_run BOOLEAN, "
            "status VARCHAR, pnl_usd FLOAT"
            ")"
        ))

    old_engine, old_session_local = storage.engine, storage.SessionLocal
    try:
        storage.engine = engine
        storage.SessionLocal = storage.sessionmaker(bind=engine)
        storage._migrate_trades_table()

        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("trades")}
        for expected in ("entry_price", "token_amount", "parent_trade_id",
                          "close_reason", "triggered_levels", "failure_reason"):
            assert expected in cols, f"міграція мала додати колонку {expected}"

        # Переконуємось, що після міграції можна реально записати рядок з новими полями
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO trades (action, status, failure_reason) VALUES ('buy', 'failed', 'test')"
            ))
            row = conn.execute(text("SELECT failure_reason FROM trades")).fetchone()
        assert row[0] == "test"
    finally:
        storage.engine, storage.SessionLocal = old_engine, old_session_local
