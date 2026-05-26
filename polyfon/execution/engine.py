"""Execution engine: runs strategies in dry or shadow mode."""
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
from rich.console import Console
from scipy import stats
from sqlalchemy import select, desc, func

from polyfon.database import session_scope
from polyfon.models import (
    DryRunSession,
    DryRunTradeResult,
    DryRunWindowResult,
    OrderBook,
    SpotPrice,
    TradeSignal,
    Window,
)
from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal
from polyfon.pricing.fair_probability import fair_probability
from polyfon.pricing.volatility import RollingVolatility
from polyfon.utils.fees import net_pnl, taker_fee_usdc


@dataclass
class FillResult:
    position_id: str
    order_class: str
    position_outcome: str
    shares: float
    entry_price: float
    notional: float
    entry_fee: float
    total_cost: float
    eval_time: datetime


@dataclass
class WindowTradeSummary:
    contract: str
    shares: float
    entry_price: float
    cost: float
    resolution: str
    settlement: float
    revenue: float
    fees: float
    pnl: float
    outcome: str
    opened_at: Optional[datetime] = None


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
    fills: List[FillResult] = field(default_factory=list)
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
        self._dry_run_session_id: Optional[str] = None

    @staticmethod
    def _naive_utc(dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    def _strategy_params_dict(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        for key, value in vars(self.strategy).items():
            if key.startswith("_"):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                params[key] = value
            else:
                params[key] = str(value)
        return params

    async def _create_dry_run_session(self, total_windows: int) -> str:
        session_id = str(uuid.uuid4())
        params = self._strategy_params_dict()
        replay_cadence = params.get("replay_cadence_seconds")
        async with session_scope() as sess:
            sess.add(
                DryRunSession(
                    id=session_id,
                    mode=self.mode,
                    strategy=self.strategy.name,
                    strategy_params_json=json.dumps(params, sort_keys=True),
                    coins_csv=",".join(self.coins) if self.coins else None,
                    window_slugs_csv=",".join(self.window_slugs) if self.window_slugs else None,
                    replay_cadence_seconds=float(replay_cadence) if replay_cadence is not None else None,
                    total_windows=total_windows,
                    processed_windows=0,
                    signaled_windows=0,
                    filled_windows=0,
                    total_trades=0,
                    total_realized_pnl=0.0,
                    started_at=self._naive_utc(datetime.now(timezone.utc)),
                    status="running",
                )
            )
        self._dry_run_session_id = session_id
        return session_id

    async def _persist_dry_window_result(self, window: Window, report: DryWindowReport, window_index: int) -> None:
        if not self._dry_run_session_id:
            return

        signal_time = None
        if report.signal_time:
            try:
                signal_time = datetime.combine(
                    window.start_et.date(),
                    datetime.strptime(report.signal_time, "%H:%M:%S").time(),
                )
            except ValueError:
                signal_time = None

        window_result_id = str(uuid.uuid4())
        async with session_scope() as sess:
            sess.add(
                DryRunWindowResult(
                    id=window_result_id,
                    dry_run_session_id=self._dry_run_session_id,
                    window_id=window.id,
                    strategy=self.strategy.name,
                    window_index=window_index,
                    status=report.status,
                    reason=report.reason,
                    signal_direction=report.signal_direction,
                    signal_edge=report.signal_edge,
                    signal_confidence=report.signal_confidence,
                    order_class=report.order_class,
                    signal_time=signal_time,
                    resolution=report.resolution,
                    realized_pnl=report.realized_pnl,
                    trade_count=len(report.trades),
                )
            )

            for fill, trade in zip(report.fills, report.trades):
                sess.add(
                    DryRunTradeResult(
                        id=str(uuid.uuid4()),
                        dry_run_window_result_id=window_result_id,
                        position_id=fill.position_id,
                        contract=trade.contract,
                        order_class=fill.order_class,
                        shares=trade.shares,
                        entry_price=trade.entry_price,
                        notional=fill.notional,
                        entry_fee=fill.entry_fee,
                        total_cost=fill.total_cost,
                        opened_at=self._naive_utc(fill.eval_time),
                        resolution=trade.resolution,
                        settlement_price=trade.settlement,
                        revenue=trade.revenue,
                        fees_paid=trade.fees,
                        pnl=trade.pnl,
                        outcome=trade.outcome,
                    )
                )

    async def _finalize_dry_run_session(
        self,
        *,
        processed_windows: int,
        signaled_windows: int,
        filled_windows: int,
        total_trades: int,
        total_realized_pnl: float,
        status: str,
        notes: Optional[str] = None,
    ) -> None:
        if not self._dry_run_session_id:
            return
        async with session_scope() as sess:
            run = await sess.get(DryRunSession, self._dry_run_session_id)
            if run:
                run.processed_windows = processed_windows
                run.signaled_windows = signaled_windows
                run.filled_windows = filled_windows
                run.total_trades = total_trades
                run.total_realized_pnl = total_realized_pnl
                run.finished_at = self._naive_utc(datetime.now(timezone.utc))
                run.status = status
                run.notes = notes

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

    def _render_dry_report(self, report: DryWindowReport, window_index: int, total_windows: int, *, running_pnl: float = 0.0) -> None:
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
            console.print(f"  contract: {trade.contract}")
            console.print(f"  time: {trade.opened_at.strftime('%H:%M:%S') if trade.opened_at else 'N/A'}")
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
        console.print(f"  Running PnL:  {running_pnl:.4f}")
        console.print(divider)

    @staticmethod
    def _print_statistics(
        *,
        trade_pnls: List[float],
        total_pnl: float,
        processed_windows: int,
        signaled_windows: int,
        filled_windows: int,
        total_trades: int,
        simulation_time_sec: float | None = None,
        sim_start: datetime | None = None,
        sim_end: datetime | None = None,
    ) -> None:
        console = Console()
        console.print("")
        console.print("[bold yellow]" + "=" * 72 + "[/]")
        console.print("[bold yellow]  DRY RUN STATISTICAL SUMMARY[/]")
        console.print("[bold yellow]" + "=" * 72 + "[/]")
        console.print(f"  Windows processed: {processed_windows}")
        console.print(f"  Windows signaled:  {signaled_windows}")
        console.print(f"  Windows filled:    {filled_windows}")
        console.print(f"  Total trades:      {total_trades}")
        console.print(f"  Total PnL:         {total_pnl:+.4f}")
        if simulation_time_sec is not None:
            console.print(f"  Simulation time:   {simulation_time_sec:.2f}s")
        if sim_start and sim_end:
            console.print(f"  Eval period:       {sim_start} — {sim_end}")
        console.print("-" * 72)

        n = len(trade_pnls)
        if n < 2:
            console.print("[dim]  Insufficient trades for statistics (need >= 2).[/]")
            console.print("=" * 72)
            return

        arr = np.array(trade_pnls, dtype=np.float64)
        mean_val = float(np.mean(arr))
        std_val = float(np.std(arr, ddof=1))
        median_val = float(np.median(arr))
        pnl_min = float(np.min(arr))
        pnl_max = float(np.max(arr))

        wins = int(np.sum(arr > 0))
        losses = int(np.sum(arr < 0))
        win_rate = wins / n * 100
        gross_gain = float(np.sum(arr[arr > 0]))
        gross_loss = float(np.sum(arr[arr < 0]))
        profit_factor = gross_gain / abs(gross_loss) if gross_loss != 0 else float("inf")

        sharpe = (mean_val / std_val) if std_val > 0 else 0.0

        t_stat, p_value = stats.ttest_1samp(arr, popmean=0, alternative="two-sided")
        t_stat = float(t_stat)
        p_value = float(p_value)
        # One-sided: probability that mean > 0
        p_value_one_sided = p_value / 2 if t_stat > 0 else 1.0 - p_value / 2

        # 95% CI via t-distribution
        ci_level = 0.95
        df = n - 1
        t_crit = float(stats.t.ppf((1 + ci_level) / 2, df))
        ci_margin = t_crit * std_val / np.sqrt(n)
        ci_low = mean_val - ci_margin
        ci_high = mean_val + ci_margin

        console.print(f"  Trades evaluated:  {n}")
        console.print(f"  Mean PnL:          {mean_val:+.4f}")
        console.print(f"  Median PnL:        {median_val:+.4f}")
        console.print(f"  Std Dev:           {std_val:.4f}")
        console.print(f"  Min:  {pnl_min:+.4f}   Max:  {pnl_max:+.4f}")
        console.print(f"  Sharpe (mean/std): {sharpe:.4f}")
        console.print(f"  Wins:   {wins} ({win_rate:.1f}%)")
        console.print(f"  Losses: {losses} ({100 - win_rate:.1f}%)")
        console.print(f"  Profit factor:     {profit_factor:.4f}")
        console.print("-" * 72)
        console.print("  Hypothesis test: H0 mean PnL = 0")
        console.print(f"  t-statistic:       {t_stat:.4f}")
        console.print(f"  p-value (2-sided): {p_value:.4f}")
        console.print(f"  p-value (1-sided): {p_value_one_sided:.4f}")
        console.print(f"  95% CI for mean:   [{ci_low:+.4f}, {ci_high:+.4f}]")

        # Verdict
        significant = p_value_one_sided < 0.05
        if significant and mean_val > 0:
            verdict = "STRATEGY APPEARS PROFITABLE (p < 0.05, one-sided)"
            color = "green"
        elif significant and mean_val < 0:
            verdict = "STRATEGY APPEARS UNPROFITABLE (mean < 0, p < 0.05)"
            color = "red"
        elif not significant and mean_val > 0:
            verdict = "INCONCLUSIVE (positive mean, but not statistically significant)"
            color = "yellow"
        else:
            verdict = "INCONCLUSIVE (no significant edge detected)"
            color = "yellow"

        console.print(f"[bold {color}]  VERDICT: {verdict}[/]")
        console.print("=" * 72)

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
            "BUY_NO": (window.down_token_id, "best_ask"),
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

        outcome_map = {
            "BUY_YES": "YES",
            "BUY_NO": "NO",
        }

        opened_at = eval_time or datetime.now(timezone.utc)

        return FillResult(
            position_id=str(uuid.uuid4()),
            order_class=order_class,
            position_outcome=outcome_map.get(signal.direction, "YES"),
            shares=signal.size,
            entry_price=price,
            notional=notional,
            entry_fee=fee,
            total_cost=notional + fee,
            eval_time=opened_at,
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

            fill = await self._simulate_fill(window, signal, eval_time=eval_time)
            if fill is None:
                continue
            order_class = str((signal.metadata or {}).get("order_class", "market")).lower()
            order_desc = (
                f"order=market spend={fill.notional:.4f} shares={fill.shares:.4f} fee={fill.entry_fee:.5f} total_cost={fill.total_cost:.4f}"
                if order_class == "market"
                else f"order=limit shares={fill.shares:.4f} notional={fill.notional:.4f}"
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
            report.fills.append(fill)
            if plan.stop_on_signal:
                break

        return report

    async def _finalize_dry_window(self, window: Window, report: DryWindowReport) -> List[float]:
        """Resolve and print realized PnL for one historical window.

        Polymarket simulation is long-only at entry: BUY_YES or BUY_NO.
        Synthetic short positions are intentionally not supported.
        """
        outcome = self._normalize_outcome(window.outcome)
        if outcome is None:
            report.reason = "unresolved window"
            return []

        realized_pnls: List[float] = []
        fee_rate = window.fee_rate or 0.07
        resolution_label = "YES" if outcome == "YES" else "NO"

        for fill in report.fills:
            outcome_hit = (
                outcome == fill.position_outcome
            )
            exit_price = 1.0 if outcome_hit else 0.0
            pnl_value = net_pnl(
                fill.shares,
                fill.entry_price,
                exit_price,
                fee_rate,
                fee_rate,
            )
            revenue = fill.shares * exit_price
            cost = fill.shares * fill.entry_price + taker_fee_usdc(fill.shares, fill.entry_price, fee_rate)
            exit_fee = taker_fee_usdc(fill.shares, exit_price, fee_rate)
            realized_pnls.append(pnl_value)
            win_label = "won" if outcome_hit else "lost"
            report.trades.append(
                WindowTradeSummary(
                    contract=fill.position_outcome,
                    shares=fill.shares,
                    entry_price=fill.entry_price,
                    cost=cost,
                    resolution=resolution_label,
                    settlement=exit_price,
                    revenue=revenue,
                    fees=fill.entry_fee + exit_fee,
                    pnl=pnl_value,
                    outcome=win_label,
                    opened_at=fill.eval_time,
                )
            )

        if realized_pnls:
            report.resolution = outcome
            report.realized_pnl = sum(realized_pnls)
        else:
            report.resolution = outcome

        return realized_pnls

    # ---- public API ----------------------------------------------------------

    async def run_dry(self, max_windows: int | None = None) -> None:
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

        if max_windows is not None and max_windows < len(windows):
            windows = windows[:max_windows]

        console = Console()
        console.print(f"[bold cyan]Dry run: {len(windows)} historical windows[/]")

        await self._create_dry_run_session(len(windows))

        total_pnl = 0.0
        trade_pnls: List[float] = []
        processed_windows = 0
        signaled_windows = 0
        filled_windows = 0
        total_trades = 0
        status = "completed"
        run_start = datetime.now(timezone.utc)
        sim_start = windows[0].start_et if windows else None
        sim_end = windows[-1].end_et if windows else None

        try:
            for idx, w in enumerate(windows, start=1):
                if not self._running:
                    status = "interrupted"
                    break
                label = f"{w.underlying} {_window_label(w)}"
                console.print(f"[dim]Processing window {idx}/{len(windows)}: {label}…[/]")
                report = await self._check_dry_window(w)
                realized_pnls = await self._finalize_dry_window(w, report)
                processed_windows += 1
                if report.signal_direction:
                    signaled_windows += 1
                if report.trades:
                    filled_windows += 1
                    total_trades += len(report.trades)
                if realized_pnls:
                    any_realized = True
                    total_pnl += sum(realized_pnls)
                    trade_pnls.extend(realized_pnls)
                await self._persist_dry_window_result(w, report, idx)
                self._render_dry_report(report, idx, len(windows), running_pnl=total_pnl)
        except Exception as exc:
            status = "failed"
            await self._finalize_dry_run_session(
                processed_windows=processed_windows,
                signaled_windows=signaled_windows,
                filled_windows=filled_windows,
                total_trades=total_trades,
                total_realized_pnl=total_pnl,
                status=status,
                notes=str(exc),
            )
            raise

        if self._running:
            elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
            self._print_statistics(
                trade_pnls=trade_pnls,
                total_pnl=total_pnl,
                processed_windows=processed_windows,
                signaled_windows=signaled_windows,
                filled_windows=filled_windows,
                total_trades=total_trades,
                simulation_time_sec=elapsed,
                sim_start=sim_start,
                sim_end=sim_end,
            )

        await self._finalize_dry_run_session(
            processed_windows=processed_windows,
            signaled_windows=signaled_windows,
            filled_windows=filled_windows,
            total_trades=total_trades,
            total_realized_pnl=total_pnl,
            status=status,
            notes=None if status == "completed" else "dry run stopped before processing all windows",
        )

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
