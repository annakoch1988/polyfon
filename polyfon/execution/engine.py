"""Execution engine: runs strategies in dry or shadow mode."""
import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from rich.console import Console
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

    async def _build_context(self, window: Window, eval_time: Optional[datetime] = None) -> Context:
        """Build execution context for a window.

        When *eval_time* is provided (historical replay), spot price and tau
        are computed relative to that timestamp.  Otherwise the latest live
        data is used (shadow mode).
        """
        symbol = window.underlying.upper()
        now = eval_time or datetime.now(timezone.utc)
        ctx = Context(timestamp=now)

        # Spot price closest to eval_time
        async with session_scope() as sess:
            # Prefer the spot price just before / at eval_time
            result = await sess.execute(
                select(SpotPrice)
                .where(
                    SpotPrice.symbol == symbol,
                    SpotPrice.timestamp <= now,
                )
                .order_by(desc(SpotPrice.timestamp))
                .limit(1)
            )
            sp = result.scalar_one_or_none()
            if sp is None:
                # No spot before eval_time; grab the earliest available
                result = await sess.execute(
                    select(SpotPrice)
                    .where(SpotPrice.symbol == symbol)
                    .order_by(SpotPrice.timestamp)
                    .limit(1)
                )
                sp = result.scalar_one_or_none()
            if sp:
                ctx.spot_price = sp.price
                vol = self._get_or_create_vol(symbol)
                vol.update(sp.price)
                ctx.sigma_per_minute = vol.sigma_per_minute

        async with session_scope() as sess:
            result = await sess.execute(
                select(SpotPrice)
                .where(
                    SpotPrice.symbol == symbol,
                    SpotPrice.timestamp >= window.start_et,
                )
                .order_by(SpotPrice.timestamp)
                .limit(1)
            )
            sp_open = result.scalar_one_or_none()
            if sp_open:
                ctx.window_open_price = sp_open.price

        # Latest order books per token (UP and DOWN separately).
        async with session_scope() as sess:
            result = await sess.execute(
                select(OrderBook)
                .where(
                    OrderBook.window_id == window.id,
                    OrderBook.token_id == window.up_token_id,
                )
                .order_by(desc(OrderBook.timestamp))
                .limit(1)
            )
            ob_up = result.scalar_one_or_none()
            if ob_up:
                ctx.up_best_bid = ob_up.best_bid
                ctx.up_best_ask = ob_up.best_ask

        async with session_scope() as sess:
            result = await sess.execute(
                select(OrderBook)
                .where(
                    OrderBook.window_id == window.id,
                    OrderBook.token_id == window.down_token_id,
                )
                .order_by(desc(OrderBook.timestamp))
                .limit(1)
            )
            ob_down = result.scalar_one_or_none()
            if ob_down:
                ctx.down_best_bid = ob_down.best_bid
                ctx.down_best_ask = ob_down.best_ask

        # Backward compat: best_bid / best_ask default to UP token prices.
        ctx.best_bid = ctx.up_best_bid
        ctx.best_ask = ctx.up_best_ask

        # Fair probability — use window_open_price as strike if available.
        if ctx.spot_price:
            tau = max(0, (window.end_et - now).total_seconds())
            ctx.tau_seconds = tau
            sigma = ctx.sigma_per_minute or 0.001
            if ctx.window_open_price:
                strike = ctx.window_open_price
                ctx.fair_probability = fair_probability(
                    spot=ctx.spot_price,
                    strike=strike,
                    tau_seconds=tau,
                    sigma_per_minute=sigma,
                )
            else:
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
        """Simulate a fill at the current best bid/ask.

        Uses the correct token (UP / DOWN) based on signal direction.
        """
        token_map = {
            "BUY_YES": (window.up_token_id, "best_ask"),
            "SELL_YES": (window.up_token_id, "best_bid"),
            "BUY_NO": (window.down_token_id, "best_ask"),
            "SELL_NO": (window.down_token_id, "best_bid"),
        }
        token_id, field = token_map.get(signal.direction, (None, None))
        if token_id is None:
            return

        price = None
        async with session_scope() as sess:
            result = await sess.execute(
                select(OrderBook)
                .where(
                    OrderBook.window_id == window.id,
                    OrderBook.token_id == token_id,
                )
                .order_by(desc(OrderBook.timestamp))
                .limit(1)
            )
            ob = result.scalar_one_or_none()
            if ob:
                price = getattr(ob, field, None)

        if price is None:
            return

        if signal.size < 5 or signal.size * price < 1.0:
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
        """Run strategy on a single open window (live / shadow mode)."""
        async with session_scope() as sess:
            result = await sess.execute(select(Window).where(Window.id == window_id))
            window = result.scalar_one_or_none()
            if not window or window.status != "open":
                return

        ctx = await self._build_context(window)
        signal = self.strategy.on_tick(window, ctx)
        if signal:
            await self._on_signal(window, signal)

    async def _check_dry_window(self, window: Window) -> None:
        """Run strategy on a historical window with T-10s evaluation."""
        eval_time = window.end_et - timedelta(seconds=10)
        ctx = await self._build_context(window, eval_time=eval_time)
        signal = self.strategy.on_tick(window, ctx)
        if signal:
            await self._on_signal(window, signal)
            console = Console()
            console.print(
                f"  [yellow]SIGNAL[/] {window.underlying} "
                f"{window.start_et.strftime('%H:%M')}–{window.end_et.strftime('%H:%M')}  "
                f"[bold]{signal.direction}[/] "
                f"edge={signal.expected_edge:.4f}  "
                f"conf={signal.confidence:.3f}"
            )
        else:
            console = Console()
            console.print(
                f"  [dim]SKIP[/]  {window.underlying} "
                f"{window.start_et.strftime('%H:%M')}–{window.end_et.strftime('%H:%M')} "
                f"[dim]no signal[/]"
            )

    # ---- public API ----------------------------------------------------------

    async def run_dry(self) -> None:
        """Dry mode: replay historical windows from DB at T-10s."""
        self._running = True
        async with session_scope() as sess:
            result = await sess.execute(
                select(Window).where(
                    Window.status.in_(["closed", "resolved"]),
                )
            )
            windows = result.scalars().all()

        console = Console()
        console.print(f"[bold cyan]Dry run: {len(windows)} historical windows[/]")

        for w in windows:
            if not self._running:
                break
            await self._check_dry_window(w)

        console.print("[bold green]Dry run complete.[/]")

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
