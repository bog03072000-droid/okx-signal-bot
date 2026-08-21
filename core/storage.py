"""
Просте SQLite-сховище для логу сигналів, угод і денного PnL.
"""
import datetime as dt
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime, Text,
    ForeignKey, inspect, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()
engine = create_engine("sqlite:///data/bot.db", echo=False)
SessionLocal = sessionmaker(bind=engine)


class SignalLog(Base):
    __tablename__ = "signal_log"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    raw_text = Column(Text)
    is_signal = Column(Boolean)
    action = Column(String)
    token_symbol = Column(String)
    contract_address = Column(String)
    chain = Column(String)
    confidence = Column(Float)
    reasoning = Column(Text)
    executed = Column(Boolean, default=False)
    rejection_reason = Column(Text, nullable=True)


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    action = Column(String)  # buy / sell
    token_symbol = Column(String)
    contract_address = Column(String)
    chain = Column(String)
    amount_usd = Column(Float)
    price = Column(Float, nullable=True)  # історичне поле, ніде не заповнюється — див. entry_price
    tx_hash = Column(String, nullable=True)
    dry_run = Column(Boolean, default=True)
    status = Column(String, default="pending")  # pending/confirmed/failed
    pnl_usd = Column(Float, nullable=True)  # заповнюється при закритті позиції

    # --- Поля для ladder TP/SL моніторингу (core/position_monitor.py) ---
    # entry_price — ціна USD/токен ОДРАЗУ після виконання buy (з DexScreener,
    # core/price_feed.py), звідки ladder-монітор рахує % зміни. Заповнюється
    # лише на buy-рядках; на sell-рядках залишається NULL.
    entry_price = Column(Float, nullable=True)
    # token_amount — RAW (найменші одиниці) кількість токена: скільки отримано
    # при buy, скільки продано при sell. У raw-одиницях, а не людських, щоб
    # напряму йти в amount_raw наступного часткового sell без конвертації
    # decimals (яку OKX quote вже врахував).
    token_amount = Column(Float, nullable=True)
    # parent_trade_id — на sell-рядку: id buy-рядка, з якого продано частку.
    # NULL для buy-рядків і для sell по сигналу з каналу (там немає
    # прив'язки до конкретної ladder-позиції).
    parent_trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)
    # close_reason — напр. "stop_loss_-10pct", "take_profit_+60pct" — щоб в
    # /history було видно, що продаж стався автоматично, а не по сигналу.
    close_reason = Column(String, nullable=True)
    # triggered_levels — JSON-список кодів рівнів, які вже спрацювали для ЦІЄЇ
    # buy-позиції (заповнюється тільки на buy-рядку). Захист від повторного
    # спрацювання того самого рівня на наступній перевірці.
    triggered_levels = Column(Text, nullable=True, default="[]")


def _migrate_trades_table():
    """
    Легка ручна міграція для SQLite: Base.metadata.create_all() створює нові
    ТАБЛИЦІ, але НЕ додає нові КОЛОНКИ в уже існуючу таблицю. Якщо data/bot.db
    вже існує з попередньої версії бота (без ladder-полів) — без цього кроку
    старий файл лишиться зі старою схемою і впаде з "no such column".

    Alembic був би "правильним" рішенням для реальних міграцій, але для
    одного SQLite-файлу на цьому масштабі це невиправдана складність —
    просто перевіряємо, яких колонок бракує, і додаємо їх напряму.
    """
    inspector = inspect(engine)
    if "trades" not in inspector.get_table_names():
        return  # свіжа БД — create_all() вище вже створив таблицю з усіма полями

    existing_cols = {c["name"] for c in inspector.get_columns("trades")}
    new_columns = {
        "entry_price": "FLOAT",
        "token_amount": "FLOAT",
        "parent_trade_id": "INTEGER",
        "close_reason": "VARCHAR",
        "triggered_levels": "TEXT",
    }
    with engine.begin() as conn:
        for col_name, col_type in new_columns.items():
            if col_name not in existing_cols:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}"))


def init_db():
    import os
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(engine)
    _migrate_trades_table()


def get_session():
    return SessionLocal()
