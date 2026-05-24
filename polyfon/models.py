"""SQLAlchemy ORM models."""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, Float, String, Boolean, DateTime, ForeignKey,
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class RunSession(Base):
    """Tracks a single collector run.

    ``finished_at`` is set on graceful shutdown; a null value indicates
    the session was aborted (SIGKILL / crash).
    """
    __tablename__ = "run_sessions"

    id = Column(String, primary_key=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    windows = relationship("Window", back_populates="run_session")


class Window(Base):
    """A single 5-minute prediction market window.

    Replaces the old ``Market`` + ``Window`` split.  One row per event
    (e.g. "BTC Up or Down, 9:05-9:10PM ET") with both UP/DOWN token IDs.

    Times are stored as naive UTC but represent ET window boundaries.
    """
    __tablename__ = "windows"

    id = Column(String, primary_key=True)
    slug = Column(String, unique=True, nullable=False, index=True)
    title = Column(String, nullable=False)
    underlying = Column(String, nullable=False)  # BTC, ETH
    start_et = Column(DateTime, nullable=False)
    end_et = Column(DateTime, nullable=False)
    outcome = Column(String, nullable=True)  # "Yes" (UP) or "No" (DOWN), once resolved
    status = Column(String, default="pending")  # pending, open, closed, resolved, invalid
    invalid_reason = Column(String, nullable=True)
    invalidated_at = Column(DateTime, nullable=True)
    run_session_id = Column(String, ForeignKey("run_sessions.id"), nullable=True)

    # Polymarket internal metadata (needed for WS subscription + fees)
    up_token_id = Column(String, nullable=False)
    down_token_id = Column(String, nullable=False)
    condition_id = Column(String, nullable=False)
    fee_rate = Column(Float, default=0.07)
    tick_size = Column(Float, default=0.01)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    run_session = relationship("RunSession", back_populates="windows")
    order_books = relationship("OrderBook", back_populates="window", cascade="all, delete-orphan")
    trade_signals = relationship("TradeSignal", back_populates="window", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="window", cascade="all, delete-orphan")


class SpotPrice(Base):
    __tablename__ = "spot_prices"

    id = Column(String, primary_key=True)
    symbol = Column(String, nullable=False)  # BTC, ETH
    price = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    source = Column(String, default="binance")
    created_at = Column(DateTime, default=func.now())


class OrderBook(Base):
    __tablename__ = "order_books"

    id = Column(String, primary_key=True)
    window_id = Column(String, ForeignKey("windows.id"), nullable=True)
    token_id = Column(String, nullable=False)
    best_bid = Column(Float, nullable=True)
    best_ask = Column(Float, nullable=True)
    bid_size = Column(Float, nullable=True)
    ask_size = Column(Float, nullable=True)
    last_trade_price = Column(Float, nullable=True)
    stale = Column(Boolean, default=False)
    timestamp = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())

    window = relationship("Window", back_populates="order_books")


class TradeSignal(Base):
    __tablename__ = "trade_signals"

    id = Column(String, primary_key=True)
    strategy = Column(String, nullable=False)
    window_id = Column(String, ForeignKey("windows.id"), nullable=False)
    direction = Column(String, nullable=False)  # BUY_YES, SELL_YES, BUY_NO, SELL_NO
    size = Column(Float, nullable=False)
    expected_edge = Column(Float, nullable=False)
    confidence = Column(Float, nullable=True)
    timestamp = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())

    window = relationship("Window", back_populates="trade_signals")


class Position(Base):
    __tablename__ = "positions"

    id = Column(String, primary_key=True)
    mode = Column(String, nullable=False)  # dry, shadow, wet
    window_id = Column(String, ForeignKey("windows.id"), nullable=True)
    strategy = Column(String, nullable=True)
    side = Column(String, nullable=False)  # LONG_YES, LONG_NO, SHORT_YES
    entry_price = Column(Float, nullable=False)
    size = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    fees_paid = Column(Float, default=0.0)
    status = Column(String, default="open")  # open, closed
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    window = relationship("Window", back_populates="positions")


class ConfigKV(Base):
    __tablename__ = "config"

    id = Column(String, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(String, nullable=False)
