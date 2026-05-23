"""Execution engine: runs strategies in dry or shadow mode."""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import select, desc

from polyfon.database import session_scope
from polyfon.models import Window, OrderBook, SpotPrice, Position, TradeSignal
from polyfon.strategies.base import BaseStrategy, Context, Signal
from polyfon.pricing.fair_probability import fair_probability
from polyfon.pricing.volatility import RollingVolatility
from polyfon.utils.fees import taker_fee_usdc


class ExecutionEngine:
    """Orchestrate strategy execution for a given mode.

    Dry mode: uses historical data from DB.
    Shadow mode: runs in real-time with simulated fills.
    """

    def __init__(self, mode: str, strategy: BaseStrategy, coins: Optional[List[str]] = None):
        self.mode = mode
        self.strategy = strategy
        self.coins = coins or []
        self._running = False
        self._vols: Dict[str, RollingVolatility] = {}

    def _get_or_create_vol(self, symbol: str) -> RollingVolatility:
        if symbol not in self._vols:
            self._vols[symbol] = RollingVolatility(window=60, interval_sec=1.0)
        return self._vols[symbol]

    async def _build_context(self, window: Window) -> Context:
        """Build execution context for a window by loading latest data."""
        symbol = window.underlying.upper()
        ctx = Context(timestamp=datetime.now(timezone.utc))

        # Latest spot price
        async with session_scope() as sess:
            result = await sess.execute(
                select(SpotPrice)
                .where(SpotPrice.symbol == symbol)
                .order_by(desc(SpotPrice.timestamp))
                .limit(1)
            )
            sp = result.scalar_one_or_none()
            if sp:
                ctx.spot_price = sp.price
                vol = self._get_or_create_vol(symbol)
                vol.update(sp.price)
                ctx.sigma_per_minute = vol.sigma_per_minute

        # Latest order book for this window
        async with session_scope() as sess:
            result = await sess.execute(
                select(OrderBook)
                .where(OrderBook.window_id == window.id)
                .order_by(desc(OrderBook.timestamp))
                .limit(1)
            )
            ob = result.scalar_one_or_none()
            if ob:
                ctx.best_bid = ob.best_bid
                ctx.best_ask = ob.best_ask

        # Fair probability — "Up or Down" markets have no fixed strike,
        # so compute using the window's time to expiry.
        if ctx.spot_price:
            now = datetime.now(timezone.utc)
            tau = max(0, (window.end_et - now).total_seconds())
            ctx.tau_seconds = tau
            sigma = ctx.sigma_per_minute or 0.001
            # For binary "Up or Down" markets, the strike is the current spot
            # at window open.  Since we don't have that here yet, we use a
            # simplified model: fair prob = 0.5 (placeholder).
            ctx.fair_probability = 0.5

        return ctx

    async def _on_signal(self, window: Window, signal: Signal) -> None:
        """Handle a strategy signal: log it, and simulate a position if dry/shadow."""
        async with session_scope() as sess:
            ts = TradeSignal(
                id=str(uuid.uuid4()),
                strategy=signal.strategy,
                window_id=window.id,
                direction=signal.direction,
                size=signal.size,
                expected_edge=signal.expected_edge,
                confidence=signal.confidence,
                timestamp=datetime.now(timezone.utc),
            )
            sess.add(ts)

        if self.mode in ("dry", "shadow"):
            await self._simulate_fill(window, signal)

    async def _simulate_fill(self, window: Window, signal: Signal) -> None:
        """Simulate a fill at the current best bid/ask."""
        price = None
        if "BUY" in signal.direction:
            async with session_scope() as sess:
                result = await sess.execute(
                    select(OrderBook)
                    .where(OrderBook.window_id == window.id)
                    .order_by(desc(OrderBook.timestamp))
                    .limit(1)
                )
                ob = result.scalar_one_or_none()
                price = ob.best_ask if ob else None
        else:
            async with session_scope() as sess:
                result = await sess.execute(
                    select(OrderBook)
                    .where(OrderBook.window_id == window.id)
                    .order_by(desc(OrderBook.timestamp))
                    .limit(1)
                )
                ob = result.scalar_one_or_none()
                price = ob.best_bid if ob else None

        if price is None:
            return

        fee_rate = window.fee_rate or 0.07
        fee = taker_fee_usdc(signal.size, price, fee_rate)

        side_map = {
            "BUY_YES": "LONG_YES",
            "SELL_YES": "SHORT_YES",
            "BUY_NO": "LONG_NO",
            "SELL_NO": "SHORT_NO",
        }

        async with session_scope() as sess:
            pos = Position(
                id=str(uuid.uuid4()),
                mode=self.mode,
                window_id=window.id,
                strategy=signal.strategy,
                side=side_map.get(signal.direction, "LONG_YES"),
                entry_price=price,
                size=signal.size,
                fees_paid=fee,
                status="open",
                opened_at=datetime.now(timezone.utc),
            )
            sess.add(pos)

    async def _check_window(self, window_id: str) -> None:
        """Run strategy on a single open window."""
        async with session_scope() as sess:
            result = await sess.execute(select(Window).where(Window.id == window_id))
            window = result.scalar_one_or_none()
            if not window or window.status != "open":
                return

        ctx = await self._build_context(window)
        signal = self.strategy.on_tick(window, ctx)
        if signal:
            await self._on_signal(window, signal)

    # ---- public API ----------------------------------------------------------

    async def run_dry(self) -> None:
        """Dry mode: replay all historical open windows from DB."""
        self._running = True
        async with session_scope() as sess:
            result = await sess.execute(
                select(Window).where(Window.status == "open")
            )
            windows = result.scalars().all()

        for w in windows:
            if not self._running:
                break
            await self._check_window(w.id)

    async def run_shadow(self) -> None:
        """Shadow mode: real-time loop over open windows."""
        self._running = True
        while self._running:
            async with session_scope() as sess:
                result = await sess.execute(
                    select(Window).where(Window.status == "open")
                )
                windows = result.scalars().all()

            for w in windows:
                if not self._running:
                    break
                await self._check_window(w.id)

            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
