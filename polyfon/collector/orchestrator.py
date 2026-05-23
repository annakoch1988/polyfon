"""Orchestrator that ties together discovery, spot, book, and window management."""
import asyncio
import logging
import signal
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.table import Table
from sqlalchemy import delete, or_, select

from polyfon.config import settings
from polyfon.database import session_scope
from polyfon.models import Window, SpotPrice, OrderBook, RunSession
from polyfon.collector.market_discovery import PolymarketDiscovery
from polyfon.collector.spot_collector import BinanceSpotCollector
from polyfon.collector.book_collector import PolymarketBookCollector

ET_TZ = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)
console = Console()


def _fmt_et(dt: datetime) -> str:
    """Format a naive-UTC datetime as a short ET-hour string."""
    return dt.replace(tzinfo=timezone.utc).astimezone(ET_TZ).strftime("%I:%M %p ET").lstrip("0")


def _next_window_boundary_et(now_utc: datetime) -> datetime:
    """Next 5-min clock boundary in ET (returned as timezone-aware UTC)."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_et = now_utc.astimezone(ET_TZ)
    minute = (now_et.minute // 5) * 5
    boundary_et = now_et.replace(minute=minute, second=0, microsecond=0)
    if abs((now_et - boundary_et).total_seconds()) <= 1.0:
        return boundary_et.astimezone(timezone.utc)
    return (boundary_et + timedelta(minutes=5)).astimezone(timezone.utc)


class CollectionOrchestrator:
    def __init__(self, coins: Optional[List[str]] = None):
        self.coins = [c.upper() for c in (coins or settings.coin_list)]
        self.discovery = PolymarketDiscovery()
        self.spot = BinanceSpotCollector(
            coins=self.coins,
            on_price=self._on_spot_price,
        )
        self.book = PolymarketBookCollector(
            on_book=self._on_book,
            on_resolution=self._on_resolution,
            carry_timeout_sec=5.0,
        )
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._discovered_windows: Dict[str, Window] = {}  # slug -> Window

        self._spot_queue: asyncio.Queue = asyncio.Queue(maxsize=50000)
        self._book_queue: asyncio.Queue = asyncio.Queue(maxsize=50000)
        self._session_id: Optional[str] = None
        self._session_started_at: Optional[datetime] = None

        # Cache: token_id -> window_id for the hot book path
        self._token_to_window: Dict[str, str] = {}
        self._book_active_tokens: set[str] = set()

    # ---- persistence callbacks + workers ---------------------------------------

    def _on_spot_price(self, symbol: str, price: float, ts: datetime) -> None:
        try:
            self._spot_queue.put_nowait((symbol, price, ts))
        except asyncio.QueueFull:
            logger.warning("Spot queue full – dropping tick for %s", symbol)

    async def _spot_worker(self) -> None:
        while self._running:
            try:
                symbol, price, ts = await asyncio.wait_for(
                    self._spot_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            async with session_scope() as sess:
                sess.add(
                    SpotPrice(
                        id=str(uuid.uuid4()),
                        symbol=symbol.upper(),
                        price=price,
                        timestamp=ts,
                        source="binance",
                    )
                )
            self._spot_queue.task_done()

    def _on_book(
        self,
        token_id: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
        bid_size: Optional[float],
        ask_size: Optional[float],
        last_trade_price: Optional[float],
        ts: datetime,
    ) -> None:
        window_id = self._token_to_window.get(token_id)
        try:
            self._book_queue.put_nowait(
                (token_id, window_id, best_bid, best_ask, bid_size, ask_size, last_trade_price, ts)
            )
        except asyncio.QueueFull:
            logger.warning("Book queue full – dropping update for %s", token_id)

    async def _book_worker(self) -> None:
        BATCH_SIZE = 500
        while self._running:
            batch: list = []
            try:
                batch.append(await asyncio.wait_for(self._book_queue.get(), timeout=1.0))
            except asyncio.TimeoutError:
                continue

            while len(batch) < BATCH_SIZE:
                try:
                    batch.append(self._book_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            async with session_scope() as sess:
                for token_id, window_id, best_bid, best_ask, bid_size, ask_size, ltp, ts in batch:
                    sess.add(
                        OrderBook(
                            id=str(uuid.uuid4()),
                            window_id=window_id,
                            token_id=token_id,
                            best_bid=best_bid,
                            best_ask=best_ask,
                            bid_size=bid_size,
                            ask_size=ask_size,
                            last_trade_price=ltp,
                            stale=False,
                            timestamp=ts,
                        )
                    )
            for _ in batch:
                self._book_queue.task_done()

    # ---- progress logging -----------------------------------------------------

    def _log_status(self) -> None:
        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        active = [w for w in self._discovered_windows.values() if w.status == "open"]
        if not active:
            console.print(
                f"[dim]{now_str}[/] Collecting [bold]{', '.join(self.coins)}[/]  "
                f"| [dim]waiting for next window[/]"
            )
        else:
            parts = []
            for w in active:
                parts.append(f"{w.underlying} {_fmt_et(w.start_et)}–{_fmt_et(w.end_et)}")
            console.print(
                f"[dim]{now_str}[/] Collecting [bold]{', '.join(self.coins)}[/]  "
                f"| [green]{'  '.join(parts)}[/]"
            )

    # ---- window management ----------------------------------------------------

    async def _sync_windows(self) -> None:
        """Discover events and upsert Window records."""
        now_n = self._naive_utc(datetime.now(timezone.utc))
        events = await self.discovery.discover_crypto_5min(coins=self.coins)

        async with session_scope() as sess:
            for ev in events:
                slug = ev["slug"]
                result = await sess.execute(select(Window).where(Window.slug == slug))
                existing = result.scalar_one_or_none()

                if existing:
                    existing.title = ev["title"]
                    existing.underlying = ev["underlying"]
                    existing.start_et = ev["start_utc"]
                    existing.end_et = ev["end_utc"]
                    existing.up_token_id = ev["up_token_id"]
                    existing.down_token_id = ev["down_token_id"]
                    existing.condition_id = ev["condition_id"]
                    existing.fee_rate = ev["fee_rate"]
                    existing.tick_size = ev["tick_size"]
                    self._discovered_windows[slug] = existing
                else:
                    # Skip windows whose slot started too long ago —
                    # they'll never be opened by the window manager.
                    if ev["start_utc"] < now_n - timedelta(seconds=1):
                        continue
                    win = Window(
                        id=str(uuid.uuid4()),
                        slug=slug,
                        title=ev["title"],
                        underlying=ev["underlying"],
                        start_et=ev["start_utc"],
                        end_et=ev["end_utc"],
                        up_token_id=ev["up_token_id"],
                        down_token_id=ev["down_token_id"],
                        condition_id=ev["condition_id"],
                        fee_rate=ev["fee_rate"],
                        tick_size=ev["tick_size"],
                        status="pending",
                        run_session_id=self._session_id,
                    )
                    self._discovered_windows[slug] = win
                    sess.add(win)

    @staticmethod
    def _naive_utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _log_window(self, action: str, win: Window) -> None:
        color = {"OPEN": "green", "CLOSED": "red"}.get(action, "white")
        console.print(
            f"  [{color}]{action:6}[/] {win.underlying} "
            f"{_fmt_et(win.start_et)}–{_fmt_et(win.end_et)}  "
            f"[dim]{win.slug}[/]  [{color}]{win.title}[/]"
        )

    async def _responsive_sleep(self, seconds: float) -> None:
        for _ in range(int(seconds)):
            if not self._running:
                return
            await asyncio.sleep(1)
        remainder = seconds - int(seconds)
        if remainder > 0 and self._running:
            await asyncio.sleep(remainder)

    async def _window_manager(self) -> None:
        """Timer-driven window open/close at 5-min ET clock boundaries.

        Sleeps precisely to each boundary, opens the current window and
        closes the previous one, then sleeps 5 min to the next boundary.
        No polling, no grace windows.
        """
        while self._running:
            now = datetime.now(timezone.utc)
            next_boundary = _next_window_boundary_et(now)
            dt = (next_boundary - now).total_seconds()
            if dt > 0:
                await self._responsive_sleep(dt)
            if not self._running:
                break

            now_n = self._naive_utc(next_boundary)
            changed = False

            async with session_scope() as sess:
                # Close open windows ending at this boundary
                result = await sess.execute(
                    select(Window).where(
                        Window.status == "open",
                        Window.end_et >= now_n - timedelta(seconds=1),
                        Window.end_et <= now_n + timedelta(seconds=1),
                    )
                )
                for win in result.scalars():
                    win.status = "closed"
                    self._discovered_windows[win.slug] = win
                    self._token_to_window.pop(win.up_token_id, None)
                    self._token_to_window.pop(win.down_token_id, None)
                    self._log_window("CLOSED", win)
                    changed = True

                # Open pending windows starting at this boundary
                result = await sess.execute(
                    select(Window).where(
                        Window.status == "pending",
                        Window.start_et >= now_n - timedelta(seconds=1),
                        Window.start_et <= now_n + timedelta(seconds=1),
                    )
                )
                for win in result.scalars():
                    win.status = "open"
                    self._discovered_windows[win.slug] = win
                    self._token_to_window[win.up_token_id] = win.id
                    self._token_to_window[win.down_token_id] = win.id
                    self._log_window("OPEN", win)
                    changed = True

            # Update book subscription when tokens change.
            # Keep unresolved windows subscribed (including closed ones)
            # so market_resolved WebSocket events are still received.
            if changed:
                all_ids = list(self._token_to_window.keys())
                async with session_scope() as sess:
                    result = await sess.execute(
                        select(Window).where(
                            Window.outcome.is_(None),
                            Window.underlying.in_(self.coins),
                        )
                    )
                    for win in result.scalars():
                        all_ids.extend([win.up_token_id, win.down_token_id])
                all_ids = list(set(all_ids))
                if self._book_active_tokens and set(all_ids) != self._book_active_tokens:
                    await self.book.update_assets(all_ids)
                    self._book_active_tokens = set(all_ids)

    # ---- shutdown -------------------------------------------------------------

    def _shutdown(self) -> None:
        console.print("\n[bold red]Shutdown signal received — stopping...[/]")
        self._running = False

    # ---- public API -----------------------------------------------------------

    async def run(self) -> None:
        self._running = True

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown)
            except NotImplementedError:
                pass

        # Create a run session for this collector start
        async with session_scope() as sess:
            self._session_started_at = self._naive_utc(datetime.now(timezone.utc))
            session = RunSession(
                id=str(uuid.uuid4()),
                started_at=self._session_started_at,
            )
            sess.add(session)
            self._session_id = session.id

        now = datetime.now(timezone.utc)
        now_n = self._naive_utc(now)

        console.print("\n[bold cyan]Maintenance[/]")

        # 1. Delete unfinished windows (pending + open) from other runs.
        removed = 0
        async with session_scope() as sess:
            result = await sess.execute(
                select(Window).where(
                    Window.status.in_(["pending", "open"]),
                    Window.run_session_id != self._session_id,
                    Window.run_session_id.isnot(None),
                )
            )
            for win in result.scalars():
                removed += 1
                self._discovered_windows.pop(win.slug, None)
                self._token_to_window.pop(win.up_token_id, None)
                self._token_to_window.pop(win.down_token_id, None)
                underlying = win.underlying
                start_fmt = _fmt_et(win.start_et)
                end_fmt = _fmt_et(win.end_et)
                console.print(
                    f"  [red]REMOVED[/] {underlying} {start_fmt}–{end_fmt}  (run: {win.run_session_id})"
                )
                await sess.delete(win)
        if not removed:
            console.print("  [dim]No unfinished windows to remove[/]")

        # 2. Resolve closed-but-unresolved windows via API.
        resolved = await self._resolve_orphans()
        if not resolved:
            console.print("  [dim]No orphan windows to resolve[/]")

        # 3. Discover and create pending windows for the current run.
        await self._sync_windows()

        # Load any existing windows into the cache
        async with session_scope() as sess:
            result = await sess.execute(
                select(Window).where(
                    Window.status.in_(["pending", "open"]),
                    Window.underlying.in_(self.coins),
                )
            )
            for win in result.scalars():
                self._discovered_windows[win.slug] = win
                if win.status == "open":
                    self._token_to_window[win.up_token_id] = win.id
                    self._token_to_window[win.down_token_id] = win.id

        # Log discovered future windows
        async with session_scope() as sess:
            result = await sess.execute(
                select(Window).where(
                    Window.underlying.in_(self.coins),
                    Window.end_et > now_n,
                ).order_by(Window.start_et)
            )
            for win in result.scalars():
                console.print(
                    f"  [cyan]DISCOVERED[/] {win.underlying} "
                    f"{_fmt_et(win.start_et)}–{_fmt_et(win.end_et)}  "
                    f"[dim]{win.slug}[/]  "
                    f"[dim]https://polymarket.com/event/{win.slug}[/]"
                )

        # Mid-window check
        next_boundary = _next_window_boundary_et(now)
        if now < next_boundary:
            wait_sec = (next_boundary - now).total_seconds()
            mid_slot = None
            mid_underlying = None
            async with session_scope() as sess:
                result = await sess.execute(
                    select(Window).where(
                        Window.underlying.in_(self.coins),
                        Window.start_et <= now_n,
                        Window.end_et > now_n,
                    ).limit(1)
                )
                mid = result.scalar_one_or_none()
                if mid:
                    mid_slot = f"{_fmt_et(mid.start_et)}–{_fmt_et(mid.end_et)}"
                    mid_underlying = mid.underlying
            session_start_str = _fmt_et(self._session_started_at)
            msg = f"\n[yellow]Session started at {session_start_str}  (run: {self._session_id})"
            if mid_slot:
                msg += f"  |  Mid-window (skipping): {mid_underlying} {mid_slot}"
            msg += f"  |  Next boundary in {wait_sec:.0f}s at {_fmt_et(next_boundary)}[/]"
            console.print(msg)

        self.spot.start()

        # Start book collector immediately so it can receive
        # market_resolved events even during the mid-window wait.
        async with session_scope() as sess:
            result = await sess.execute(
                select(Window).where(
                    Window.status.in_(["pending", "open"]),
                    Window.underlying.in_(self.coins),
                )
            )
            initial_ids = [t for w in result.scalars() for t in (w.up_token_id, w.down_token_id)]
        if initial_ids:
            self.book.start(initial_ids)
            self._book_active_tokens = set(initial_ids)

        self._tasks.append(asyncio.create_task(self._spot_worker()))
        self._tasks.append(asyncio.create_task(self._book_worker()))
        self._tasks.append(asyncio.create_task(self._window_manager()))

        ticks = 0
        while self._running:
            await asyncio.sleep(1)
            ticks += 1
            if ticks >= 60 and self._running:
                ticks = 0
                await self._sync_windows()
                await self._resolve_orphans()
            if ticks % 30 == 0 and self._running:
                self._log_status()

    def _on_resolution(self, token_id: str, outcome: str) -> None:
        """Handle a market_resolved WebSocket event.

        Looks up the window by token_id in the DB — the in-memory
        ``_token_to_window`` cache is only live while a window is open,
        but resolution events can arrive minutes later.
        """
        asyncio.create_task(self._apply_resolution(token_id, outcome))

    async def _apply_resolution(self, token_id: str, outcome: str) -> None:
        async with session_scope() as sess:
            result = await sess.execute(
                select(Window).where(
                    Window.status.in_(["open", "closed"]),
                    Window.outcome.is_(None),
                    or_(Window.up_token_id == token_id, Window.down_token_id == token_id),
                )
            )
            w = result.scalar_one_or_none()
            if w:
                w.outcome = outcome
                w.status = "resolved"
                console.print(
                    f"  [green]RESOLVED[/] {w.underlying} "
                    f"{_fmt_et(w.start_et)}–{_fmt_et(w.end_et)}  "
                    f"[bold]{outcome}[/]  "
                    f"(run: {w.run_session_id})  "
                    f"[dim]https://polymarket.com/event/{w.slug}[/]"
                )

    async def _resolve_orphans(self) -> int:
        """Resolve closed-but-unresolved windows via Gamma API.

        Returns the number of windows resolved.
        """
        async with session_scope() as sess:
            result = await sess.execute(
                select(Window).where(
                    Window.outcome.is_(None),
                    Window.status == "closed",
                )
            )
            orphans = result.scalars().all()

        resolved = 0
        for win in orphans:
            outcome = await self.discovery.fetch_resolution(win.slug)
            if outcome:
                resolved += 1
                async with session_scope() as sess:
                    w = await sess.get(Window, win.id)
                    if w and w.outcome is None:
                        w.outcome = outcome
                        w.status = "resolved"
                        console.print(
                            f"  [green]RESOLVED (API)[/] {w.underlying} "
                            f"{_fmt_et(w.start_et)}–{_fmt_et(w.end_et)}  "
                            f"[bold]{outcome}[/]  "
                            f"(run: {w.run_session_id})  "
                            f"[dim]https://polymarket.com/event/{w.slug}[/]"
                        )
        return resolved

    async def stop(self) -> None:
        self._running = False
        if self._session_id:
            async with session_scope() as sess:
                session = await sess.get(RunSession, self._session_id)
                if session:
                    session.finished_at = self._naive_utc(datetime.now(timezone.utc))
        await self.spot.stop()
        await self.book.stop()
        for t in self._tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
