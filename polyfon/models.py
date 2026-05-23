"""SQLAlchemy ORM models."""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, Integer, Float, String, Boolean, DateTime, ForeignKey,
    create_engine, event
)
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Market(Base):
    __tablename__ = "markets"

    id = Column(String, primary_key=True)
    condition_id = Column(String, nullable=False)
    token_id = Column(String, unique=True, nullable=False)
    slug = Column(String, nullable=True)
    title = Column(String, nullable=False)
    category = Column(String, nullable=False, default="crypto")
    fees_enabled = Column(Boolean, default=True)
    fee_rate = Column(Float, default=0.07)
    tick_size = Column(Float, default=0.01)
    neg_risk = Column(Boolean, default=False)
    underlying = Column(String, nullable=False)  # BTC, ETH, etc.
    strike = Column(Float, nullable=True)
    resolution_time = Column(DateTime, nullable=True)
    status = Column(String, default="active")  # active, closed, resolved
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    windows = relationship("Window", back_populates="market", cascade="all, delete-orphan")
    order_books = relationship("OrderBook", back_populates="market")
    positions = relationship("Position", back_populates="market")
    fee_params = relationship("FeeParams", back_populates="market", uselist=False)


class Window(Base):
    __tablename__ = "windows"

    id = Column(String, primary_key=True)
    market_id = Column(String, ForeignKey("markets.id"), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    resolution_time = Column(DateTime, nullable=True)
    strike = Column(Float, nullable=False)
    outcome = Column(String, nullable=True)  # YES, NO, UNRESOLVED
    status = Column(String, default="open")  # open, closed, resolving, resolved
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    market = relationship("Market", back_populates="windows")
    order_books = relationship("OrderBook", back_populates="window")
    positions = relationship("Position", back_populates="window")
    trade_signals = relationship("TradeSignal", back_populates="window")


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
    market_id = Column(String, ForeignKey("markets.id"), nullable=False)
    window_id = Column(String, ForeignKey("windows.id"), nullable=True)
    best_bid = Column(Float, nullable=True)
    best_ask = Column(Float, nullable=True)
    bid_size = Column(Float, nullable=True)
    ask_size = Column(Float, nullable=True)
    last_trade_price = Column(Float, nullable=True)
    stale = Column(Boolean, default=False)
    timestamp = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())

    market = relationship("Market", back_populates="order_books")
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
    market_id = Column(String, ForeignKey("markets.id"), nullable=False)
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

    market = relationship("Market", back_populates="positions")
    window = relationship("Window", back_populates="positions")


class FeeParams(Base):
    __tablename__ = "fee_params"

    id = Column(String, primary_key=True)
    market_id = Column(String, ForeignKey("markets.id"), unique=True, nullable=False)
    fee_rate = Column(Float, default=0.07)
    maker_rate = Column(Float, default=0.0)
    rebate_rate = Column(Float, default=0.20)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    market = relationship("Market", back_populates="fee_params")


class ConfigKV(Base):
    __tablename__ = "config"

    id = Column(String, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(String, nullable=False)
