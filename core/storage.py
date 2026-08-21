"""
Просте SQLite-сховище для логу сигналів, угод і денного PnL.
"""
import datetime as dt
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime, Text
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
    price = Column(Float, nullable=True)
    tx_hash = Column(String, nullable=True)
    dry_run = Column(Boolean, default=True)
    status = Column(String, default="pending")  # pending/confirmed/failed
    pnl_usd = Column(Float, nullable=True)  # заповнюється при закритті позиції


def init_db():
    import os
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()
