"""Execution engine: runs strategies in dry or shadow mode."""
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from rich.console import Console
from sqlalchemy import select, desc, func

from polyfon.database import session_scope
from polyfon.models import Window, OrderBook, SpotPrice, Position, TradeSignal
from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal
from polyfon.pricing.fair_probability import fair_probability
from polyfon.pricing.volatility import RollingVolatility
from polyfon.utils.fees import net_pnl, taker_fee_usdc


@dataclass
class FillResult:
    position_id: str
    order_class: str
    side: str
    shares: float
    entry_price: float
    notional: float
    entry_fee: float
    total_cost: float
    eval_time: datetime


@dataclass
class WindowTradeSummary:
    side: str
    shares: float
    entry_price: float
    cost: float
    resolution: str
    settlement: float
    revenue: float
    fees: float
    pnl: float
    outcome: str


@dataclass
class DryWindowReport:
    window_id: str
    title: str
    slug: str
    underlying: str
    window_label: str
    link: str
    status: str = "NO SIGNAL"
    reason: Optional[str] = None
    signal_direction: Optional[str] = None
    signal_edge: Optional[float] = None
    signal_confidence: Optional[float] = None
    order_class: Optional[str] = None
    order_desc: Optional[str] = None
    signal_time: Optional[str] = None
    trades: List[WindowTradeSummary] = field(default_factory=list)
    resolution: Optional[str] = None
    realized_pnl: float = 0.0


def _window_label(window: Window) -> str:
    return f"{window.start_et.strftime('%H:%M')}–{window.end_et.strftime('%H:%M')}"


def _window_link(window: Window) -> str:
    return f"https://polymarket.com/event/{window.slug}"


class ExecutionEngine:
    """Orchestrate strategy execution for a given mode.

    Dry mode: uses historical data from DB.
    Shadow mode: runs in real-time with simulated fills.
    """

    def __init__(self, mode: str, strategy: BaseStrategy, coins: Optional[List[str]] = None, window_slugs: Optional[List[str]] = None):
        self.mode = mode
        self.strategy = strategy
        self.coins = coins or []
        self.window_slugs = window_slugs or []
        self._running = False
        self._vols: Dict[str, RollingVolatility] = {}

    def _get_or_create_vol(self, symbol: str) -> RollingVolatility:
        if symbol not in self._vols:
            self._vols[symbol] = RollingVolatility(window=60, interval_sec=1.0)
        return self._vols[symbol]

    def _init_dry_report(self, window: Window) -> DryWindowReport:
        return DryWindowReport(
            window_id=window.id,
            title=window.title,
            slug=window.slug,
            underlying=window.underlying,
            window_label=_window_label(window),
            link=_window_link(window),
        )

    def _render_dry_report(self, report: DryWindowReport, window_index: int, total_windows: int) -> None:
        console = Console()
        divider = "=" * 72
        console.print(divider)
        console.print(f"[bold]Window {window_index}/{total_windows}[/]")
        console.print(f"[bold]{report.underlying} {report.window_label}[/]")
        console.print(f"Slug: {report.slug}")
        console.print(f"Title: {report.title}")
        console.print(f"Link: {report.link}")
        console.print("-" * 72)
        console.print(f"Status: {report.status}")

        if report.reason:
            console.print(f"Reason: {report.reason}")

        if report.signal_direction:
            console.print(f"Action: {report.signal_direction}")
            console.print("Signal:")
            if report.signal_edge is not None:
                console.print(f"  edge: {report.signal_edge:.4f}")
            if report.signal_confidence is not None:
                console.print(f"  conf: {report.signal_confidence:.4f}")
            if report.order_desc:
                console.print(f"  {report.order_desc}")
            if report.signal_time:
                console.print(f"  timestamp: {report.signal_time}")

        for trade in report.trades:
            console.print("")
            console.print("Trade:")
            console.print(f"  side: {trade.side}")
            console.print(f"  shares: {trade.shares:.4f}")
            console.print(f"  entry: {trade.entry_price:.4f}")
            console.print(f"  cost: {trade.cost:.4f}")
            console.print("")
            console.print("Settlement:")
            console.print(f"  resolution: {trade.resolution}")
            console.print(f"  settlement: {trade.settlement:.4f}")
            console.print(f"  revenue: {trade.revenue:.4f}")
            console.print(f"  fees: {trade.fees:.5f}")
            console.print(f"  pnl: {trade.pnl:.4f}")
            console.print(f"  outcome: {trade.outcome}")

        console.print("")
        console.print("Result:")
        if report.trades:
            console.print(f"  Trades: {len(report.trades)}")
        else:
            console.print("  No fills")
        console.print(f"  Realized PnL: {report.realized_pnl:.4f}")
        console.print(divider)

    @staticmethod
    def _normalize_outcome(outcome: Optional[str]) -> Optional[str]:
        if outcome is None:
            return None
        normalized = outcome.strip().upper()
        if normalized in {"YES", "UP"}:
            return "YES"
        if normalized in {"NO", "DOWN"}:
            return "NO"
        return normalized

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
            spot_q = (
                select(SpotPrice)
                .where(
                    SpotPrice.symbol == symbol,
                    SpotPrice.timestamp >= window.start_et,
                    SpotPrice.timestamp <= now,
                )
                .order_by(SpotPrice.timestamp)
            )
            result = await sess.execute(spot_q.limit(1))
            sp_open = result.scalar_one_or_none()
            if sp_open:
                ctx.window_open_price = sp_open.price

            # Range high/low within the window (for ROM).
            range_result = await sess.execute(
                select(func.max(SpotPrice.price), func.min(SpotPrice.price))
                .where(
                    SpotPrice.symbol == symbol,
                    SpotPrice.timestamp >= window.start_et,
                    SpotPrice.timestamp <= now,
                )
            )
            row = range_result.one_or_none()
            if row and row[0] is not None:
                ctx.range_high = row[0]
                ctx.range_low = row[1]

        # Order books at or before eval_time for historical alignment.
        async with session_scope() as sess:
            q_up = (
                select(OrderBook)
                .where(
                    OrderBook.window_id == window.id,
                    OrderBook.token_id == window.up_token_id,
                )
            )
            if eval_time:
                q_up = q_up.where(OrderBook.timestamp <= now)
            result = await sess.execute(q_up.order_by(desc(OrderBook.timestamp)).limit(1))
            ob_up = result.scalar_one_or_none()
            if ob_up:
                ctx.up_best_bid = ob_up.best_bid
                ctx.up_best_ask = ob_up.best_ask

        async with session_scope() as sess:
            q_down = (
                select(OrderBook)
                .where(
                    OrderBook.window_id == window.id,
                    OrderBook.token_id == window.down_token_id,
                )
            )
            if eval_time:
                q_down = q_down.where(OrderBook.timestamp <= now)
            result = await sess.execute(q_down.order_by(desc(OrderBook.timestamp)).limit(1))
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

    async def _on_signal(self, window: Window, signal: Signal, eval_time: Optional[datetime] = None) -> Optional[FillResult]:
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
            return await self._simulate_fill(window, signal, eval_time=eval_time)
        return None

    async def _simulate_fill(self, window: Window, signal: Signal, eval_time: Optional[datetime] = None) -> Optional[FillResult]:
        """Simulate a fill at the best bid/ask.

        For historical replay (eval_time set), uses the latest book at
        or before eval_time.  For live (shadow), uses the latest book.
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
            return None

        price = None
        async with session_scope() as sess:
            query = (
                select(OrderBook)
                .where(
                    OrderBook.window_id == window.id,
                    OrderBook.token_id == token_id,
                )
            )
            if eval_time:
                query = query.where(OrderBook.timestamp <= eval_time)
            result = await sess.execute(
                query.order_by(desc(OrderBook.timestamp)).limit(1)
            )
            ob = result.scalar_one_or_none()
            if ob:
                price = getattr(ob, field, None)

        if price is None:
            return None

        order_class = "market"
        if signal.metadata:
            order_class = str(signal.metadata.get("order_class", "market")).lower()

        notional = signal.size * price
        if order_class == "limit":
            if signal.size < 5 or notional < 1.0:
                return None
        else:
            if notional < 1.0:
                return None

        fee_rate = window.fee_rate or 0.07
        fee = taker_fee_usdc(signal.size, price, fee_rate)

        side_map = {
            "BUY_YES": "LONG_YES",
            "SELL_YES": "SHORT_YES",
            "BUY_NO": "LONG_NO",
            "SELL_NO": "SHORT_NO",
        }

        async with session_scope() as sess:
            position_id = str(uuid.uuid4())
            opened_at = eval_time or datetime.now(timezone.utc)
            pos = Position(
                id=position_id,
                mode=self.mode,
                window_id=window.id,
                strategy=signal.strategy,
                side=side_map.get(signal.direction, "LONG_YES"),
                entry_price=price,
                size=signal.size,
                fees_paid=fee,
                status="open",
                opened_at=opened_at,
            )
            sess.add(pos)

        return FillResult(
            position_id=position_id,
            order_class=order_class,
            side=side_map.get(signal.direction, "LONG_YES"),
            shares=signal.size,
            entry_price=price,
            notional=notional,
            entry_fee=fee,
            total_cost=notional + fee,
            eval_time=eval_time or datetime.now(timezone.utc),
        )

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

    async def _check_dry_window(self, window: Window) -> DryWindowReport:
        """Run strategy on a historical window using its replay plan."""
        report = self._init_dry_report(window)
        plan = self.strategy.build_replay_plan(window)
        if not isinstance(plan, ReplayPlan) or not plan.eval_times:
            report.reason = "no replay plan"
            return report

        for eval_time in plan.eval_times:
            if not self._running:
                return report
            ctx = await self._build_context(window, eval_time=eval_time)
            signal = self.strategy.on_tick(window, ctx)
            if not signal:
                continue

            fill = await self._on_signal(window, signal, eval_time=eval_time)
            order_class = str((signal.metadata or {}).get("order_class", "market")).lower()
            order_desc = (
                f"order=market spend={fill.notional:.4f} shares={fill.shares:.4f} fee={fill.entry_fee:.5f} total_cost={fill.total_cost:.4f}"
                if fill and order_class == "market"
                else f"order=limit shares={signal.size:.4f} notional={((signal.metadata or {}).get('notional', 0.0)):.4f}"
                if order_class == "limit"
                else "order=unfilled"
            )
            report.status = "SIGNAL"
            report.reason = None
            report.signal_direction = signal.direction
            report.signal_edge = signal.expected_edge
            report.signal_confidence = signal.confidence
            report.order_class = order_class
            report.order_desc = order_desc
            report.signal_time = eval_time.strftime('%H:%M:%S')
            if plan.stop_on_signal:
                return report

        report.reason = "no signal"
        return report

    async def _finalize_dry_window(self, window: Window, report: DryWindowReport) -> List[float]:
        """Resolve and print realized PnL for one historical window."""
        outcome = self._normalize_outcome(window.outcome)
        if outcome is None:
            report.reason = "unresolved window"
            return []

        realized_pnls: List[float] = []
        async with session_scope() as sess:
            result = await sess.execute(
                select(Position)
                .where(
                    Position.mode == "dry",
                    Position.window_id == window.id,
                    Position.status == "open",
                )
                .order_by(Position.opened_at)
            )
            positions = result.scalars().all()

            for pos in positions:
                outcome_hit = (
                    (outcome == "YES" and pos.side == "LONG_YES")
                    or (outcome == "NO" and pos.side == "LONG_NO")
                )
                exit_price = 1.0 if outcome_hit else 0.0
                fee_rate = window.fee_rate or 0.07
                pnl_value = net_pnl(
                    pos.size,
                    pos.entry_price,
                    exit_price,
                    fee_rate,
                    fee_rate,
                )
                revenue = pos.size * exit_price
                cost = pos.size * pos.entry_price + taker_fee_usdc(pos.size, pos.entry_price, fee_rate)
                exit_fee = taker_fee_usdc(pos.size, exit_price, fee_rate)
                pos.exit_price = exit_price
                pos.pnl = pnl_value
                pos.status = "closed"
                pos.closed_at = datetime.now(timezone.utc)
                realized_pnls.append(pnl_value)
                resolution_label = "YES" if outcome == "YES" else "NO"
                win_label = "won" if outcome_hit else "lost"
                report.trades.append(
                    WindowTradeSummary(
                        side=pos.side,
                        shares=pos.size,
                        entry_price=pos.entry_price,
                        cost=cost,
                        resolution=resolution_label,
                        settlement=exit_price,
                        revenue=revenue,
                        fees=pos.fees_paid + exit_fee,
                        pnl=pnl_value,
                        outcome=win_label,
                    )
                )

        if realized_pnls:
            report.resolution = outcome
            report.realized_pnl = sum(realized_pnls)
        else:
            report.resolution = outcome

        return realized_pnls

    # ---- public API ----------------------------------------------------------

    async def run_dry(self) -> None:
        """Dry mode: replay historical windows from DB using strategy plans."""
        self._running = True
        async with session_scope() as sess:
            query = select(Window).where(
                Window.status.in_(["closed", "resolved"]),
            )
            if self.coins:
                query = query.where(Window.underlying.in_(self.coins))
            if self.window_slugs:
                query = query.where(Window.slug.in_(self.window_slugs))
            result = await sess.execute(query)
            windows = result.scalars().all()

        console = Console()
        console.print(f"[bold cyan]Dry run: {len(windows)} historical windows[/]")

        total_pnl = 0.0
        any_realized = False
        for idx, w in enumerate(windows, start=1):
            if not self._running:
                break
            label = f"{w.underlying} {_window_label(w)}"
            console.print(f"[dim]Processing window {idx}/{len(windows)}: {label}…[/]")
            report = await self._check_dry_window(w)
            realized_pnls = await self._finalize_dry_window(w, report)
            if realized_pnls:
                any_realized = True
                total_pnl += sum(realized_pnls)
            self._render_dry_report(report, idx, len(windows))

        if self._running:
            if any_realized:
                color = "green" if total_pnl >= 0 else "red"
                console.print(f"[bold {color}]Total realized PnL: {total_pnl:.4f}[/]")
            else:
                console.print("[dim]No realized PnL (no fills or unresolved windows).[/]")

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
