"""Orchestrator that ties together discovery, spot, book, and window management."""
import asyncio
import logging
import signal
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from polyfon.config import settings
from polyfon.database import session_scope
from polyfon.models import Market, Window, SpotPrice, OrderBook
from polyfon.collector.market_discovery import PolymarketDiscovery
from polyfon.collector.spot_collector import BinanceSpotCollector
from polyfon.collector.book_collector import PolymarketBookCollector

logger = logging.getLogger(__name__)
console = Console()


def _polymarket_url(condition_id: str, slug: Optional[str]) -> str:
    if slug:
        return f"https://polymarket.com/event/{slug}"
    return f"https://polymarket.com/event/{condition_id}"


def _next_window_boundary(now: datetime) -> datetime:
    minute = (now.minute // 5) * 5
    current_start = now.replace(minute=minute, second=0, microsecond=0)
    if abs((now - current_start).total_seconds()) <= 1.0:
        return current_start
    return current_start + timedelta(minutes=5)


@dataclass(frozen=True)
class _SpotItem:
    symbol: str
    price: float
    ts: datetime


@dataclass(frozen=True)
class _BookItem:
    market_id: str
    window_id: Optional[str]
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    last_trade_price: Optional[float]
    ts: datetime


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
            carry_timeout_sec=5.0,
        )
        self._markets: Dict[str, Dict] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._book_active_tokens: set[str] = set()
        self._book_paused: bool = True

        self._spot_queue: asyncio.Queue[_SpotItem] = asyncio.Queue(maxsize=50000)
        self._book_queue: asyncio.Queue[_BookItem] = asyncio.Queue(maxsize=50000)

        # In-memory caches to avoid DB lookups on the hot book path.
        self._market_id_by_token: Dict[str, str] = {}
        self._window_id_by_market: Dict[str, Optional[str]] = {}
        # Active windows deduped by slug (each event has 2 token_ids → 2 windows)
        self._active_windows: Dict[str, Dict[str, Any]] = {}

    # ---- persistence callbacks + workers ----------------------------------------

    def _on_spot_price(self, symbol: str, price: float, ts: datetime) -> None:
        try:
            self._spot_queue.put_nowait(_SpotItem(symbol, price, ts))
        except asyncio.QueueFull:
            logger.warning("Spot queue full – dropping tick for %s", symbol)

    async def _spot_worker(self) -> None:
        while self._running:
            try:
                item = await asyncio.wait_for(self._spot_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            async with session_scope() as sess:
                sess.add(
                    SpotPrice(
                        id=str(uuid.uuid4()),
                        symbol=item.symbol.upper(),
                        price=item.price,
                        timestamp=item.ts,
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
        market_id = self._market_id_by_token.get(token_id)
        if not market_id:
            return
        window_id = self._window_id_by_market.get(market_id)
        try:
            self._book_queue.put_nowait(
                _BookItem(market_id, window_id, best_bid, best_ask, bid_size, ask_size, last_trade_price, ts)
            )
        except asyncio.QueueFull:
            logger.warning("Book queue full – dropping update for %s", token_id)

    async def _book_worker(self) -> None:
        BATCH_SIZE = 500
        while self._running:
            batch: list[_BookItem] = []
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
                for item in batch:
                    sess.add(
                        OrderBook(
                            id=str(uuid.uuid4()),
                            market_id=item.market_id,
                            window_id=item.window_id,
                            best_bid=item.best_bid,
                            best_ask=item.best_ask,
                            bid_size=item.bid_size,
                            ask_size=item.ask_size,
                            last_trade_price=item.last_trade_price,
                            stale=False,
                            timestamp=item.ts,
                        )
                    )
            for _ in batch:
                self._book_queue.task_done()

    # ---- low-frequency progress logging ---------------------------------------

    def _log_status(self) -> None:
        now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if not self._active_windows:
            console.print(
                f"[dim]{now_str}[/] Collecting [bold]{', '.join(self.coins)}[/]  "
                f"| [dim]waiting for next window[/]"
            )
        else:
            parts = []
            for slug, info in sorted(self._active_windows.items()):
                parts.append(
                    f"{info['underlying']} {info['start'].strftime('%H:%M')}–{info['end'].strftime('%H:%M')}"
                )
            console.print(
                f"[dim]{now_str}[/] Collecting [bold]{', '.join(self.coins)}[/]  "
                f"| [green]{'  '.join(parts)}[/]"
            )

    # ---- market & window management ------------------------------------------

    async def _sync_markets(self) -> None:
        markets = await self.discovery.discover_crypto_5min(coins=self.coins)
        async with session_scope() as sess:
            for m in markets:
                tid = m.get("token_id")
                cid = m.get("condition_id")
                if not tid:
                    continue
                self._markets[tid] = m
                result = await sess.execute(select(Market).where(Market.token_id == tid))
                existing = result.scalar_one_or_none()
                if existing:
                    self._market_id_by_token[tid] = existing.id
                    existing.slug = m.get("slug") or existing.slug
                    existing.title = m.get("title") or existing.title
                    existing.underlying = m.get("underlying") or self._extract_underlying(m)
                    existing.strike = m.get("strike") if m.get("strike") is not None else self._extract_strike(m)
                    existing.resolution_time = self._extract_resolution_time(m)
                else:
                    underlying = m.get("underlying") or self._extract_underlying(m)
                    strike = m.get("strike") if m.get("strike") is not None else self._extract_strike(m)
                    market = Market(
                        id=str(uuid.uuid4()),
                        condition_id=cid or tid,
                        token_id=tid,
                        slug=m.get("slug"),
                        title=m.get("title") or "Unknown",
                        category=m.get("category", "crypto").lower(),
                        fees_enabled=True,
                        fee_rate=float(m.get("fee_rate", 0.07)),
                        tick_size=float(m.get("tick_size", 0.01)),
                        neg_risk=m.get("neg_risk", False),
                        underlying=underlying,
                        strike=strike,
                        resolution_time=self._extract_resolution_time(m),
                        status="active",
                    )
                    self._market_id_by_token[tid] = market.id
                    sess.add(market)

    def _extract_underlying(self, market: Dict) -> str:
        title = (market.get("title") or "").upper()
        for c in self.coins:
            if c in title:
                return c
        return "BTC"

    def _extract_strike(self, market: Dict) -> Optional[float]:
        title = market.get("title") or ""
        if "up or down" in title.lower():
            return None
        import re
        m = re.search(r"\$?([0-9,]+(?:\.[0-9]+)?)", title)
        if m:
            return float(m.group(1).replace(",", ""))
        return None

    def _extract_resolution_time(self, market: Dict) -> Optional[datetime]:
        ts = market.get("resolution_time") or market.get("end_date_iso") or market.get("endDate")
        if ts:
            try:
                if isinstance(ts, str):
                    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                pass
        slug = market.get("slug", "")
        import re
        m = re.search(r"-([0-9]{10})$", slug)
        if m:
            return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        return None

    @staticmethod
    def _market_window(market: Market) -> Optional[tuple[datetime, datetime]]:
        if not market.resolution_time:
            return None
        end = market.resolution_time
        start = end - timedelta(minutes=5)
        return start, end

    async def _open_market_windows(self, now: datetime) -> None:
        async with session_scope() as sess:
            result = await sess.execute(
                select(Market).where(Market.status == "active", Market.underlying.in_(self.coins))
            )
            markets = result.scalars().all()

            for market in markets:
                slot = self._market_window(market)
                if not slot:
                    continue
                start, end = slot
                # SQLite strips tz on write; assume UTC if naive
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                if not (start <= now < end):
                    continue

                res_existing = await sess.execute(
                    select(Window).where(
                        Window.market_id == market.id,
                        Window.status == "open",
                    )
                )
                if res_existing.scalar_one_or_none():
                    continue

                win = Window(
                    id=str(uuid.uuid4()),
                    market_id=market.id,
                    start_time=start,
                    end_time=end,
                    strike=market.strike or 0.0,
                    status="open",
                )
                self._window_id_by_market[market.id] = win.id
                sess.add(win)
                if market.slug:
                    self._active_windows[market.slug] = {
                        "underlying": market.underlying,
                        "title": market.title,
                        "start": start,
                        "end": end,
                    }

    async def _close_expired_windows(self, now: datetime) -> None:
        async with session_scope() as sess:
            res = await sess.execute(
                select(Window, Market)
                .join(Market, Window.market_id == Market.id)
                .where(
                    Window.status == "open",
                    Window.end_time <= now,
                )
            )
            for win, market in res.all():
                self._window_id_by_market.pop(market.id, None)
                win.status = "closed"
                if market.slug:
                    self._active_windows.pop(market.slug, None)

            stale_res = await sess.execute(
                select(Market).where(
                    Market.status == "active",
                    Market.resolution_time <= now - timedelta(minutes=5),
                )
            )
            for mkt in stale_res.scalars():
                mkt.status = "closed"
                win_res = await sess.execute(
                    select(Window).where(
                        Window.market_id == mkt.id,
                        Window.status == "open",
                    )
                )
                for stale_win in win_res.scalars():
                    self._window_id_by_market.pop(mkt.id, None)
                    stale_win.status = "closed"

    async def _show_skipped(self) -> None:
        async with session_scope() as sess:
            res = await sess.execute(
                select(Window, Market)
                .join(Market, Window.market_id == Market.id)
                .where(Window.status == "open")
            )
            rows = list(res.all())
            if rows:
                console.print(
                    f"\n[bold yellow]\u26a0 {len(rows)} in-progress window(s) "
                    f"from previous run will be skipped[/]"
                )

    async def _responsive_sleep(self, seconds: float) -> None:
        """Sleep up to *seconds* but return early if _running becomes False."""
        for _ in range(int(seconds)):
            if not self._running:
                return
            await asyncio.sleep(1)
        remainder = seconds - int(seconds)
        if remainder > 0 and self._running:
            await asyncio.sleep(remainder)

    async def _window_manager(self) -> None:
        while self._running:
            now = datetime.now(timezone.utc)
            await self._close_expired_windows(now)
            await self._open_market_windows(now)
            await self._responsive_sleep(10)

    def _shutdown(self) -> None:
        console.print("\n[bold red]\u26a0 Shutdown signal received — stopping...[/]")
        self._running = False

    # ---- public API ----------------------------------------------------------

    async def run(self) -> None:
        self._running = True

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown)
            except NotImplementedError:
                pass

        await self._sync_markets()

        now = datetime.now(timezone.utc)

        table = Table(title="Discovered 5-min Crypto Markets", show_lines=True)
        table.add_column("Underlying", style="cyan")
        table.add_column("Strike", style="magenta")
        table.add_column("Title", style="green")
        table.add_column("Polymarket URL", style="blue")

        async with session_scope() as sess:
            result = await sess.execute(
                select(Market).where(
                    Market.status == "active",
                    Market.underlying.in_(self.coins),
                    Market.resolution_time > now,
                )
            )
            db_markets = result.scalars().all()
            seen_slugs: set[str] = set()
            for market in db_markets:
                if market.slug in seen_slugs:
                    continue
                seen_slugs.add(market.slug)
                url = _polymarket_url(market.condition_id, market.slug)
                table.add_row(
                    market.underlying,
                    str(market.strike) if market.strike else "\u2014",
                    market.title,
                    url,
                )

        console.print(table)

        # If we're mid-window, show which one and wait for the next boundary
        next_boundary = _next_window_boundary(now)
        if now < next_boundary:
            wait_sec = (next_boundary - now).total_seconds()
            mid_title = None
            async with session_scope() as sess:
                res = await sess.execute(
                    select(Market).where(
                        Market.status == "active",
                        Market.underlying.in_(self.coins),
                    )
                )
                for mkt in res.scalars():
                    slot = self._market_window(mkt)
                    if not slot:
                        continue
                    s, e = slot
                    if s.tzinfo is None:
                        s = s.replace(tzinfo=timezone.utc)
                    if e.tzinfo is None:
                        e = e.replace(tzinfo=timezone.utc)
                    if s <= now < e:
                        mid_title = mkt.title
                        break
            msg = f"\n[bold yellow]Mid-window start — waiting {wait_sec:.0f}s until next boundary"
            if mid_title:
                msg += f" ({mid_title})"
            msg += "[/]"
            console.print(msg)

        self.spot.start()

        now = datetime.now(timezone.utc)
        await self._close_expired_windows(now)
        await self._show_skipped()

        self._tasks.append(asyncio.create_task(self._spot_worker()))
        self._tasks.append(asyncio.create_task(self._book_worker()))
        self._tasks.append(asyncio.create_task(self._window_manager()))
        self._tasks.append(asyncio.create_task(self._book_resume_loop()))

        ticks = 0
        while self._running:
            await asyncio.sleep(1)
            ticks += 1
            if ticks >= 60 and self._running:
                ticks = 0
                await self._sync_markets()
            if ticks % 30 == 0 and self._running:
                self._log_status()

    async def _book_resume_loop(self) -> None:
        while self._running:
            now = datetime.now(timezone.utc)
            next_boundary = _next_window_boundary(now)
            if now < next_boundary:
                await self._responsive_sleep((next_boundary - now).total_seconds())
            else:
                await self._responsive_sleep(1)
                continue

            if not self._running:
                break

            current_ids = list(self._markets.keys())
            if current_ids:
                if not self._book_active_tokens:
                    console.print(
                        f"\n[bold green]\u25b6 Starting book collection at {next_boundary.strftime('%H:%M:%S')} UTC[/]"
                    )
                    self.book.start(current_ids)
                elif self._book_active_tokens != set(current_ids):
                    console.print(
                        f"\n[bold blue]\u21bb Updating book subscriptions at {next_boundary.strftime('%H:%M:%S')} UTC[/]"
                    )
                    await self.book.update_assets(current_ids)
                self._book_active_tokens = set(current_ids)
                self._book_paused = False

            now = datetime.now(timezone.utc)
            next_boundary = _next_window_boundary(now)
            await self._responsive_sleep(max(1.0, (next_boundary - now).total_seconds()))

    async def stop(self) -> None:
        self._running = False
        await self.spot.stop()
        await self.book.stop()
        for t in self._tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
