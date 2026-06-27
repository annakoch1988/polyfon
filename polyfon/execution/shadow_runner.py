"""Low-latency in-memory shadow runner.

Shadow mode is intended to behave as close to wet mode as possible while never
sending real orders. Decisions are made directly from in-memory live state fed
by the websocket collectors; database persistence is strictly a background side
effect for audit / later analysis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Optional

from rich.console import Console

from polyfon.collector.book_collector import PolymarketBookCollector
from polyfon.collector.market_discovery import PolymarketDiscovery
from polyfon.collector.orchestrator import ET_TZ, _fmt_et, _next_window_boundary_et
from polyfon.collector.spot_collector import BinanceSpotCollector
from polyfon.database import session_scope
from polyfon.models import OrderBook, Position, ShadowRunSession, SpotPrice, TradeSignal, Window
from polyfon.pricing.fair_probability import fair_probability
from polyfon.pricing.volatility import RollingVolatility
from polyfon.strategies.base import BaseStrategy, Context, Signal as StrategySignal
from polyfon.utils.clock import make_clock
from polyfon.utils.fees import net_pnl, taker_fee_usdc

logger = logging.getLogger(__name__)
console = Console()


@dataclass(slots=True)
class LiveBookState:
    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    last_trade_price: Optional[float] = None
    stale: bool = False
    timestamp: Optional[datetime] = None


@dataclass(slots=True)
class SpotObservation:
    price: float
    timestamp: datetime


@dataclass(slots=True)
class OpenShadowPosition:
    id: str
    window_id: str
    strategy: str
    contract: str
    direction: str
    size: float
    entry_price: float
    entry_fee: float
    opened_at: datetime
    expected_edge: float
    confidence: float


@dataclass(slots=True)
class WindowRuntimeState:
    window: Window
    spot_history: deque[SpotObservation] = field(default_factory=lambda: deque(maxlen=4096))
    resolved_signal: bool = False

    @property
    def symbol(self) -> str:
        return self.window.underlying.upper()

    @property
    def window_open_price(self) -> Optional[float]:
        return self.spot_history[0].price if self.spot_history else None

    @property
    def range_high(self) -> Optional[float]:
        return max((obs.price for obs in self.spot_history), default=None)

    @property
    def range_low(self) -> Optional[float]:
        return min((obs.price for obs in self.spot_history), default=None)

    @property
    def mean_spot_price(self) -> Optional[float]:
        if not self.spot_history:
            return None
        return mean(obs.price for obs in self.spot_history)


class ShadowRunner:
    """In-memory event-driven shadow runner.

    - owns its own live collectors
    - evaluates on every relevant state change
    - simulates fills immediately from in-memory best ask
    - persists audit data asynchronously in the background
    """

    def __init__(self, strategy: BaseStrategy, coins: Optional[list[str]] = None):
        self.strategy = strategy
        self.coins = [c.upper() for c in (coins or [])]
        self.discovery = PolymarketDiscovery()
        self._clock = make_clock("system")
        self.spot = BinanceSpotCollector(
            coins=self.coins,
            on_price=self._on_spot_price,
            on_disconnect=self._on_spot_disconnect,
        )
        self.book = PolymarketBookCollector(
            on_book=self._on_book,
            on_resolution=self._on_resolution,
            on_disconnect=self._on_book_disconnect,
            carry_timeout_sec=5.0,
        )
        self._running = False
        self._tasks: list[asyncio.Task] = []

        self._windows_by_id: dict[str, WindowRuntimeState] = {}
        self._windows_by_slug: dict[str, WindowRuntimeState] = {}
        self._token_to_window_id: dict[str, str] = {}
        self._latest_spot: dict[str, SpotObservation] = {}
        self._latest_books: dict[str, LiveBookState] = {}
        self._vols: dict[str, RollingVolatility] = {}
        self._vols_short: dict[str, RollingVolatility] = {}
        self._recent_spot: dict[str, deque[SpotObservation]] = defaultdict(lambda: deque(maxlen=7200))

        self._open_positions_by_window: dict[str, OpenShadowPosition] = {}
        self._persist_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=100000)
        self._needs_eval: set[str] = set()
        self._book_active_tokens: set[str] = set()
        self._latest_book_persist_payload: dict[str, dict[str, Any]] = {}
        self._book_flush_scheduled = False
        self._last_persist_warning_at: Optional[datetime] = None
        self._shadow_run_session_id: Optional[str] = None
        self._total_windows_seen = 0
        self._processed_windows = 0
        self._signaled_windows = 0
        self._filled_windows = 0
        self._total_trades = 0
        self._total_realized_pnl = 0.0
        self._resolved_window_ids: set[str] = set()

    # ------------------------------------------------------------------
    # lifecycle

    async def run(self) -> None:
        self._running = True
        try:
            await self._clock.start()
        except Exception:
            logger.exception("Clock start failed; falling back to system time")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown)
            except NotImplementedError:
                pass

        await self._sync_windows(initial=True)
        await self._create_shadow_run_session()
        await self._refresh_book_subscription()

        self.spot.start()

        self._tasks = [
            asyncio.create_task(self._window_manager(), name="shadow-window-manager"),
            asyncio.create_task(self._resolution_poll_loop(), name="shadow-resolution-poller"),
            asyncio.create_task(self._persistence_worker(), name="shadow-persistence-worker"),
            asyncio.create_task(self._evaluation_loop(), name="shadow-evaluation-loop"),
        ]

        console.print(
            f"[bold green]Shadow mode live[/] strategy={self.strategy.name} coins={', '.join(self.coins)}"
        )

        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self._running and not self._tasks:
            return
        self._running = False
        await self.spot.stop()
        try:
            await self._clock.stop()
        except Exception:
            pass
        await self.book.stop()
        await self._finalize_shadow_run_session(status="completed")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    def _shutdown(self) -> None:
        console.print("\n[bold red]Shutdown signal received — stopping shadow...[/]")
        self._running = False

    # ------------------------------------------------------------------
    # live callbacks

    def _on_spot_price(self, symbol: str, price: float, ts: datetime) -> None:
        ts = self._naive_utc(ts)
        obs = SpotObservation(price=price, timestamp=ts)
        symbol = symbol.upper()
        self._latest_spot[symbol] = obs
        self._recent_spot[symbol].append(obs)

        vol = self._vols.setdefault(symbol, RollingVolatility(window=60, interval_sec=1.0))
        vol.update(price)
        vol_short = self._vols_short.setdefault(symbol, RollingVolatility(window=15, interval_sec=1.0))
        vol_short.update(price)

        for runtime in self._windows_by_id.values():
            w = runtime.window
            if w.underlying.upper() != symbol:
                continue
            if w.status != "open":
                continue
            if ts < w.start_et:
                continue
            runtime.spot_history.append(obs)
            self._needs_eval.add(w.id)

        self._enqueue_persist("spot", {
            "symbol": symbol,
            "price": price,
            "timestamp": ts,
        })

    def _on_book(
        self,
        token_id: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
        bid_size: Optional[float],
        ask_size: Optional[float],
        last_trade_price: Optional[float],
        ts: datetime,
        stale: bool = False,
    ) -> None:
        ts = self._naive_utc(ts)
        state = LiveBookState(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            last_trade_price=last_trade_price,
            stale=stale,
            timestamp=ts,
        )
        self._latest_books[token_id] = state
        window_id = self._token_to_window_id.get(token_id)
        if window_id:
            self._needs_eval.add(window_id)
        self._latest_book_persist_payload[token_id] = {
            "token_id": token_id,
            "window_id": window_id,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "last_trade_price": last_trade_price,
            "timestamp": ts,
            "stale": stale,
        }
        if not self._book_flush_scheduled:
            self._book_flush_scheduled = True
            asyncio.create_task(self._flush_book_persist_buffer())

    def _on_resolution(self, token_id: str, outcome: str) -> None:
        asyncio.create_task(self._apply_resolution(token_id, outcome))

    def _on_spot_disconnect(self, coins: list[str], ts: datetime, reason: str) -> None:
        console.print(f"  [bold red]SPOT DISCONNECT[/] {','.join(coins)} [dim]{reason}[/]")

    def _on_book_disconnect(self, asset_ids: list[str], ts: datetime, reason: str) -> None:
        console.print(f"  [bold red]BOOK DISCONNECT[/] {len(asset_ids)} assets [dim]{reason}[/]")

    # ------------------------------------------------------------------
    # windows / discovery

    async def _sync_windows(self, initial: bool = False) -> None:
        events = await self.discovery.discover_crypto_5min(coins=self.coins)
        now_n = self._naive_utc(self._clock.now_utc())

        for ev in events:
            slug = ev["slug"]
            runtime = self._windows_by_slug.get(slug)
            if runtime is None:
                win = Window(
                    id=str(uuid.uuid4()),
                    slug=slug,
                    title=ev["title"],
                    underlying=ev["underlying"].upper(),
                    start_et=ev["start_utc"],
                    end_et=ev["end_utc"],
                    outcome=ev.get("outcome"),
                    status="pending",
                    run_session_id=None,
                    up_token_id=ev["up_token_id"],
                    down_token_id=ev["down_token_id"],
                    condition_id=ev["condition_id"],
                    fee_rate=ev.get("fee_rate") or 0.07,
                    tick_size=ev.get("tick_size") or 0.01,
                )
                if win.start_et <= now_n < win.end_et:
                    win.status = "open"
                elif win.end_et <= now_n:
                    win.status = "closed" if not win.outcome else "resolved"
                elif win.outcome:
                    win.status = "resolved"
                runtime = WindowRuntimeState(window=win)
                self._windows_by_id[win.id] = runtime
                self._windows_by_slug[slug] = runtime
                self._total_windows_seen += 1
                if win.status in {"open", "closed", "pending"} and win.outcome is None:
                    self._token_to_window_id[win.up_token_id] = win.id
                    self._token_to_window_id[win.down_token_id] = win.id
                self._log_window("DISCOVERED" if initial else "SYNC", win)
            else:
                runtime.window.outcome = ev.get("outcome") or runtime.window.outcome
                if runtime.window.outcome and runtime.window.status != "resolved":
                    runtime.window.status = "resolved"

    async def _window_manager(self) -> None:
        while self._running:
            now = self._clock.now_utc()
            next_boundary = _next_window_boundary_et(now)
            delay = max((next_boundary - now).total_seconds(), 0.0)
            await asyncio.sleep(delay)
            if not self._running:
                break
            now_n = self._naive_utc(self._clock.now_utc())
            changed = False

            for runtime in list(self._windows_by_id.values()):
                w = runtime.window
                if w.status == "open" and w.end_et <= now_n:
                    w.status = "closed" if w.outcome is None else "resolved"
                    self._log_window("CLOSED", w)
                    changed = True
                elif w.status == "pending" and w.start_et <= now_n < w.end_et:
                    w.status = "open"
                    self._log_window("OPEN", w)
                    changed = True

            await self._sync_windows()
            if changed:
                await self._refresh_book_subscription()

    async def _refresh_book_subscription(self) -> None:
        asset_ids: set[str] = set()
        for runtime in self._windows_by_id.values():
            w = runtime.window
            if w.outcome is not None:
                continue
            if w.status in {"pending", "open", "closed"}:
                asset_ids.add(w.up_token_id)
                asset_ids.add(w.down_token_id)

        if not asset_ids:
            self._book_active_tokens = set()
            return

        if not self._book_active_tokens:
            self.book.start(sorted(asset_ids))
        elif asset_ids != self._book_active_tokens:
            await self.book.update_assets(sorted(asset_ids))
        self._book_active_tokens = set(asset_ids)

    # ------------------------------------------------------------------
    # evaluation / context

    async def _evaluation_loop(self) -> None:
        while self._running:
            if not self._needs_eval:
                await asyncio.sleep(0.01)
                continue
            window_id = self._needs_eval.pop()
            runtime = self._windows_by_id.get(window_id)
            if runtime is None:
                continue
            if runtime.window.status != "open":
                continue
            if runtime.window.id in self._open_positions_by_window:
                continue
            ctx = self._build_context(runtime)
            signal = self.strategy.on_tick(runtime.window, ctx)
            if signal is not None:
                await self._handle_signal(runtime, signal, ctx)

    def _build_context(self, runtime: WindowRuntimeState) -> Context:
        window = runtime.window
        now = self._clock.now_utc()
        now_n = self._naive_utc(now)
        ctx = Context(timestamp=now)

        latest_spot = self._latest_spot.get(window.underlying.upper())
        if latest_spot is not None:
            ctx.spot_price = latest_spot.price

        ctx.window_open_price = runtime.window_open_price
        ctx.range_high = runtime.range_high
        ctx.range_low = runtime.range_low
        ctx.mean_spot_price = runtime.mean_spot_price

        long_vol = self._vols.get(window.underlying.upper())
        if long_vol is not None:
            ctx.sigma_per_minute = long_vol.sigma_per_minute
        short_vol = self._vols_short.get(window.underlying.upper())
        if short_vol is not None:
            ctx.sigma_short_per_minute = short_vol.sigma_per_minute

        up = self._latest_books.get(window.up_token_id)
        if up is not None:
            ctx.up_best_bid = up.best_bid
            ctx.up_best_ask = up.best_ask
            ctx.up_bid_size = up.bid_size
            ctx.up_ask_size = up.ask_size
            ctx.best_bid = up.best_bid
            ctx.best_ask = up.best_ask

        down = self._latest_books.get(window.down_token_id)
        if down is not None:
            ctx.down_best_bid = down.best_bid
            ctx.down_best_ask = down.best_ask
            ctx.down_bid_size = down.bid_size
            ctx.down_ask_size = down.ask_size

        leader = getattr(self.strategy, "leader", None)
        if leader:
            leader = leader.upper()
            recent = self._recent_spot.get(leader)
            if recent:
                ctx.leader_spot_price = recent[-1].price
                lookback = float(getattr(self.strategy, "lookback_seconds", 60.0))
                cutoff = now - timedelta(seconds=lookback)
                older = None
                for obs in reversed(recent):
                    if obs.timestamp <= cutoff:
                        older = obs
                        break
                if older and older.price > 0:
                    ctx.leader_return = (recent[-1].price - older.price) / older.price

        tau = max(0.0, (window.end_et - now_n).total_seconds())
        ctx.tau_seconds = tau
        if ctx.spot_price is not None:
            sigma = ctx.sigma_per_minute or 0.001
            if ctx.window_open_price:
                ctx.fair_probability = fair_probability(
                    spot=ctx.spot_price,
                    strike=ctx.window_open_price,
                    tau_seconds=tau,
                    sigma_per_minute=sigma,
                )
            else:
                ctx.fair_probability = 0.5

        return ctx

    async def _handle_signal(self, runtime: WindowRuntimeState, signal: StrategySignal, ctx: Context) -> None:
        window = runtime.window
        if window.id in self._open_positions_by_window:
            return

        fill = self._simulate_fill(window, signal)
        if fill is None:
            return

        contract = "YES" if signal.direction == "BUY_YES" else "NO"
        position = OpenShadowPosition(
            id=str(uuid.uuid4()),
            window_id=window.id,
            strategy=signal.strategy,
            contract=contract,
            direction=signal.direction,
            size=signal.size,
            entry_price=fill["entry_price"],
            entry_fee=fill["entry_fee"],
            opened_at=fill["opened_at"],
            expected_edge=signal.expected_edge,
            confidence=signal.confidence,
        )
        self._open_positions_by_window[window.id] = position
        self._signaled_windows += 1
        self._filled_windows += 1
        self._total_trades += 1

        start_label = _fmt_et(window.start_et)
        end_label = _fmt_et(window.end_et)
        console.print(
            "  [bold green]SIGNAL[/] %s %s–%s  %s @ %.4f  shares=%.4f  edge=%.4f conf=%.2f"
            % (
                window.underlying,
                start_label,
                end_label,
                signal.direction,
                position.entry_price,
                position.size,
                signal.expected_edge,
                signal.confidence,
            )
        )

        self._enqueue_persist("signal", {
            "strategy": signal.strategy,
            "window_id": window.id,
            "direction": signal.direction,
            "size": signal.size,
            "expected_edge": signal.expected_edge,
            "confidence": signal.confidence,
            "timestamp": position.opened_at,
        })
        self._enqueue_persist("position_open", {
            "id": position.id,
            "mode": "shadow",
            "window_id": window.id,
            "strategy": signal.strategy,
            "contract": contract,
            "entry_price": position.entry_price,
            "size": position.size,
            "fees_paid": position.entry_fee,
            "opened_at": position.opened_at,
        })

    def _simulate_fill(self, window: Window, signal: StrategySignal) -> Optional[dict[str, Any]]:
        token_id = window.up_token_id if signal.direction == "BUY_YES" else window.down_token_id
        book = self._latest_books.get(token_id)
        if book is None or book.best_ask is None or book.stale:
            return None
        price = book.best_ask
        notional = signal.size * price
        order_class = str((signal.metadata or {}).get("order_class", "market")).lower()
        if order_class == "limit":
            if signal.size < 5 or notional < 1.0:
                return None
        elif notional < 1.0:
            return None
        fee = taker_fee_usdc(signal.size, price, window.fee_rate or 0.07)
        return {
            "entry_price": price,
            "entry_fee": fee,
            "opened_at": self._naive_utc(self._clock.now_utc()),
        }

    # ------------------------------------------------------------------
    # resolution / settlement

    async def _resolution_poll_loop(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break
            for runtime in list(self._windows_by_id.values()):
                w = runtime.window
                if w.status != "closed" or w.outcome is not None:
                    continue
                outcome = await self.discovery.fetch_resolution(w.slug)
                if outcome:
                    await self._apply_resolution_by_window(w.id, outcome)

    async def _apply_resolution(self, token_id: str, outcome: str) -> None:
        window_id = self._token_to_window_id.get(token_id)
        if not window_id:
            for runtime in self._windows_by_id.values():
                w = runtime.window
                if w.up_token_id == token_id or w.down_token_id == token_id:
                    window_id = w.id
                    break
        if window_id:
            await self._apply_resolution_by_window(window_id, outcome)

    async def _apply_resolution_by_window(self, window_id: str, outcome: str) -> None:
        runtime = self._windows_by_id.get(window_id)
        if runtime is None:
            return
        window = runtime.window
        normalized = self._normalize_outcome(outcome)
        if normalized is None or window.outcome is not None:
            return
        window.outcome = normalized
        window.status = "resolved"
        start_label = _fmt_et(window.start_et)
        end_label = _fmt_et(window.end_et)
        console.print(
            "  [green]RESOLVED[/] %s %s–%s  [bold]%s[/]"
            % (window.underlying, start_label, end_label, normalized)
        )

        position = self._open_positions_by_window.pop(window_id, None)
        if window_id not in self._resolved_window_ids:
            self._processed_windows += 1
            self._resolved_window_ids.add(window_id)
        if position is None:
            return

        hit = normalized == position.contract
        exit_price = 1.0 if hit else 0.0
        pnl = net_pnl(
            position.size,
            position.entry_price,
            exit_price,
            entry_fee_rate=window.fee_rate or 0.07,
            exit_fee_rate=window.fee_rate or 0.07,
            is_taker=True,
        )
        total_fees = position.entry_fee + taker_fee_usdc(position.size, exit_price, window.fee_rate or 0.07)
        self._total_realized_pnl += pnl

        console.print(
            f"    [cyan]SETTLED[/] {position.contract} exit={exit_price:.2f} pnl={pnl:.4f} fees={total_fees:.5f}"
        )
        console.print(
            f"    [bold magenta]RUN PNL[/] realized={self._total_realized_pnl:.4f}  "
            f"processed={self._processed_windows} signaled={self._signaled_windows} trades={self._total_trades}"
        )
        self._enqueue_persist("position_close", {
            "id": position.id,
            "exit_price": exit_price,
            "pnl": pnl,
            "fees_paid": total_fees,
            "closed_at": self._naive_utc(self._clock.now_utc()),
        })

    async def _create_shadow_run_session(self) -> None:
        session_id = str(uuid.uuid4())
        params = self._strategy_params_dict()
        async with session_scope() as sess:
            sess.add(
                ShadowRunSession(
                    id=session_id,
                    strategy=self.strategy.name,
                    strategy_params_json=json.dumps(params, sort_keys=True),
                    coins_csv=",".join(self.coins) if self.coins else None,
                    total_windows=self._total_windows_seen,
                    processed_windows=0,
                    signaled_windows=0,
                    filled_windows=0,
                    total_trades=0,
                    total_realized_pnl=0.0,
                    started_at=self._naive_utc(self._clock.now_utc()),
                    status="running",
                )
            )
        self._shadow_run_session_id = session_id

    async def _finalize_shadow_run_session(self, status: str, notes: Optional[str] = None) -> None:
        if not self._shadow_run_session_id:
            return
        async with session_scope() as sess:
            run = await sess.get(ShadowRunSession, self._shadow_run_session_id)
            if run is None:
                return
            if run.finished_at is not None:
                return
            run.total_windows = self._total_windows_seen
            run.processed_windows = self._processed_windows
            run.signaled_windows = self._signaled_windows
            run.filled_windows = self._filled_windows
            run.total_trades = self._total_trades
            run.total_realized_pnl = self._total_realized_pnl
            run.finished_at = self._naive_utc(self._clock.now_utc())
            run.status = status
            run.notes = notes

    def _strategy_params_dict(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for key, value in vars(self.strategy).items():
            if key.startswith("_"):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                params[key] = value
            else:
                params[key] = str(value)
        return params

    # ------------------------------------------------------------------
    # db persistence (background only)

    async def _flush_book_persist_buffer(self) -> None:
        await asyncio.sleep(0.25)
        payloads = list(self._latest_book_persist_payload.values())
        self._latest_book_persist_payload.clear()
        self._book_flush_scheduled = False
        for payload in payloads:
            self._enqueue_persist("book", payload)

    def _enqueue_persist(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self._persist_queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            now = datetime.now(timezone.utc)
            should_log = (
                self._last_persist_warning_at is None
                or (now - self._last_persist_warning_at).total_seconds() >= 5.0
            )
            if should_log:
                logger.warning(
                    "Shadow persistence queue full; dropping %s event(s). "
                    "Decision path is still live; only background audit persistence is lagging.",
                    kind,
                )
                self._last_persist_warning_at = now

    async def _persistence_worker(self) -> None:
        while self._running:
            try:
                kind, payload = await asyncio.wait_for(self._persist_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._persist_event(kind, payload)
            except Exception:
                logger.exception("Shadow persistence failed for %s", kind)
            finally:
                self._persist_queue.task_done()

    async def _persist_event(self, kind: str, payload: dict[str, Any]) -> None:
        async with session_scope() as sess:
            if kind == "spot":
                sess.add(
                    SpotPrice(
                        id=str(uuid.uuid4()),
                        symbol=payload["symbol"],
                        price=payload["price"],
                        timestamp=payload["timestamp"],
                        source="binance-shadow",
                    )
                )
                return

            if kind == "book":
                sess.add(
                    OrderBook(
                        id=str(uuid.uuid4()),
                        window_id=payload.get("window_id"),
                        token_id=payload["token_id"],
                        best_bid=payload.get("best_bid"),
                        best_ask=payload.get("best_ask"),
                        bid_size=payload.get("bid_size"),
                        ask_size=payload.get("ask_size"),
                        last_trade_price=payload.get("last_trade_price"),
                        stale=payload.get("stale", False),
                        timestamp=payload["timestamp"],
                    )
                )
                return

            if kind == "signal":
                sess.add(
                    TradeSignal(
                        id=str(uuid.uuid4()),
                        strategy=payload["strategy"],
                        window_id=payload["window_id"],
                        direction=payload["direction"],
                        size=payload["size"],
                        expected_edge=payload["expected_edge"],
                        confidence=payload["confidence"],
                        timestamp=payload["timestamp"],
                    )
                )
                return

            if kind == "position_open":
                sess.add(
                    Position(
                        id=payload["id"],
                        mode=payload["mode"],
                        window_id=payload["window_id"],
                        strategy=payload["strategy"],
                        contract=payload["contract"],
                        entry_price=payload["entry_price"],
                        size=payload["size"],
                        fees_paid=payload["fees_paid"],
                        status="open",
                        opened_at=payload["opened_at"],
                    )
                )
                return

            if kind == "position_close":
                position = await sess.get(Position, payload["id"])
                if position is None:
                    return
                position.exit_price = payload["exit_price"]
                position.pnl = payload["pnl"]
                position.fees_paid = payload["fees_paid"]
                position.closed_at = payload["closed_at"]
                position.status = "closed"

    # ------------------------------------------------------------------
    # helpers

    @staticmethod
    def _naive_utc(dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    @staticmethod
    def _normalize_outcome(outcome: Optional[str]) -> Optional[str]:
        if outcome is None:
            return None
        normalized = str(outcome).strip().upper()
        if normalized in {"YES", "UP"}:
            return "YES"
        if normalized in {"NO", "DOWN"}:
            return "NO"
        return None

    def _log_window(self, action: str, win: Window) -> None:
        start_label = _fmt_et(win.start_et)
        end_label = _fmt_et(win.end_et)
        console.print(
            "  [cyan]%s[/] %s %s–%s  [dim]https://polymarket.com/event/%s[/]"
            % (action, win.underlying, start_label, end_label, win.slug)
        )