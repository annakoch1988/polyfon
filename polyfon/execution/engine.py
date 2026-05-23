"""Execution engine: runs strategies in dry or shadow mode."""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import select, desc

from polyfon.database import session_scope
from polyfon.models import Window, Market, OrderBook, SpotPrice, Position, TradeSignal
from polyfon.strategies.base import BaseStrategy, Context, Signal
from polyfon.pricing.fair_probability import fair_probability
from polyfon.pricing.volatility import RollingVolatility
from polyfon.utils.fees import taker_fee_usdc, net_pnl


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

    async def _build_context(self, window, market) -> Context:
        """Build execution context for a window by loading latest data."""
        symbol = market.underlying.upper()
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

        # Latest order book
        async with session_scope() as sess:
            result = await sess.execute(
                select(OrderBook)
                .where(OrderBook.market_id == market.id)
                .order_by(desc(OrderBook.timestamp))
                .limit(1)
            )
            ob = result.scalar_one_or_none()
            if ob:
                ctx.best_bid = ob.best_bid
                ctx.best_ask = ob.best_ask

        # Fair probability
        if ctx.spot_price and market.strike:
            now = datetime.now(timezone.utc)
            if window.end_time:
                tau = max(0, (window.end_time - now).total_seconds())
            else:
                tau = 0
            ctx.tau_seconds = tau
            sigma = ctx.sigma_per_minute or 0.001
            ctx.fair_probability = fair_probability(
                spot=ctx.spot_price,
                strike=market.strike,
                tau_seconds=tau,
                sigma_per_minute=sigma,
            )

        return ctx

    async def _on_signal(self, window, market, signal: Signal) -> None:
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
            await self._simulate_fill(window, market, signal)

    async def _simulate_fill(self, window, market, signal: Signal) -> None:
        """Simulate a fill at the current best bid/ask."""
        price = None
        if "BUY" in signal.direction:
            # Buy at ask
            async with session_scope() as sess:
                result = await sess.execute(
                    select(OrderBook)
                    .where(OrderBook.market_id == market.id)
                    .order_by(desc(OrderBook.timestamp))
                    .limit(1)
                )
                ob = result.scalar_one_or_none()
                price = ob.best_ask if ob else None
        else:
            async with session_scope() as sess:
                result = await sess.execute(
                    select(OrderBook)
                    .where(OrderBook.market_id == market.id)
                    .order_by(desc(OrderBook.timestamp))
                    .limit(1)
                )
                ob = result.scalar_one_or_none()
                price = ob.best_bid if ob else None

        if price is None:
            return

        fee_rate = market.fee_rate or 0.07
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
                market_id=market.id,
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

            result2 = await sess.execute(select(Market).where(Market.id == window.market_id))
            market = result2.scalar_one_or_none()
            if not market:
                return

        ctx = await self._build_context(window, market)
        signal = self.strategy.on_tick(window, ctx)
        if signal:
            await self._on_signal(window, market, signal)

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
