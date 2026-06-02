"""Polymarket order book collection via WebSocket.

Subscribes to the public market WebSocket for real-time orderbook,
price change, and best_bid_ask events. Uses carry-forward when no
update arrives within a timeout window.

WebSocket endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Any

import websockets
from websockets import State
from websockets.exceptions import ConnectionClosed

from polyfon.config import settings

logger = logging.getLogger(__name__)


class PolymarketBookCollector:
    """Real-time order book collector using Polymarket WebSocket.

    Subscription message:
        {"assets_ids": ["<token_id_1>", ...], "type": "market", "custom_feature_enabled": true}

    Handles events:
        - book: full orderbook snapshot
        - price_change: delta updates (includes best_bid / best_ask)
        - best_bid_ask: direct best bid/ask update
        - last_trade_price: trade execution

    Carry-forward: if no message for a token within carry_timeout_sec,
    marks last known values as stale.
    """

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    PING_INTERVAL = 10.0  # seconds — server expects ping every 10s

    def __init__(
        self,
        on_book: Optional[Callable[[str, Optional[float], Optional[float],
                                     Optional[float], Optional[float],
                                     Optional[float], datetime, bool], None]] = None,
        on_resolution: Optional[Callable[[str, str], None]] = None,
        on_disconnect: Optional[Callable[[list[str], datetime, str], None]] = None,
        carry_timeout_sec: float = 5.0,
    ):
        self.on_book = on_book
        self.on_resolution = on_resolution
        self.on_disconnect = on_disconnect
        self.carry_timeout_sec = carry_timeout_sec
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._carry_task: Optional[asyncio.Task] = None
        self._assets_ids: list[str] = []
        self._ws: Optional[Any] = None
        self._last: Dict[str, Dict[str, Any]] = {}
        self._last_seen: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    # ---- state management ---------------------------------------------------

    def _update_state(
        self,
        asset_id: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
        bid_size: Optional[float] = None,
        ask_size: Optional[float] = None,
        last_trade_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Update internal state for a token and return the record."""
        record = {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "last_trade_price": last_trade_price,
            "stale": False,
            "timestamp": datetime.now(timezone.utc),
        }
        self._last[asset_id] = record
        self._last_seen[asset_id] = record["timestamp"]
        return record

    def _get_stale_record(self, asset_id: str) -> Dict[str, Any]:
        """Return a stale carry-forward record for a token."""
        prev = self._last.get(asset_id)
        ts = datetime.now(timezone.utc)
        self._last_seen[asset_id] = ts
        if prev:
            return {
                "best_bid": prev.get("best_bid"),
                "best_ask": prev.get("best_ask"),
                "bid_size": prev.get("bid_size"),
                "ask_size": prev.get("ask_size"),
                "last_trade_price": prev.get("last_trade_price"),
                "stale": True,
                "timestamp": ts,
            }
        return {
            "best_bid": None, "best_ask": None,
            "bid_size": None, "ask_size": None,
            "last_trade_price": None, "stale": True,
            "timestamp": ts,
        }

    # ---- message handlers ---------------------------------------------------

    def _handle_book(self, payload: Dict[str, Any]) -> None:
        """Handle full orderbook snapshot event."""
        asset_id = payload.get("asset_id", "")
        if not asset_id:
            return
        bids = payload.get("bids", [])
        asks = payload.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        bid_size = float(bids[0]["size"]) if bids else None
        ask_size = float(asks[0]["size"]) if asks else None
        ltp = payload.get("last_trade_price")
        ltp = float(ltp) if ltp is not None else None
        record = self._update_state(asset_id, best_bid, best_ask, bid_size, ask_size, ltp)
        self._emit(asset_id, record)

    def _handle_price_change(self, payload: Dict[str, Any]) -> None:
        """Handle price_change delta event.

        Contains an array of price_changes; each has best_bid / best_ask.
        """
        for change in payload.get("price_changes", []):
            asset_id = change.get("asset_id", "")
            if not asset_id:
                continue
            bb = change.get("best_bid")
            ba = change.get("best_ask")
            # size == "0" means level removed, so best_bid/best_ask may be 0
            best_bid = float(bb) if bb is not None and str(bb) != "0" else None
            best_ask = float(ba) if ba is not None and str(ba) != "0" else None
            # If best_bid is falsy but we had a previous value, keep it (carry-forward)
            prev = self._last.get(asset_id)
            if best_bid is None and prev:
                best_bid = prev.get("best_bid")
            if best_ask is None and prev:
                best_ask = prev.get("best_ask")
            # price_change events do not carry top-of-book bid/ask sizes.
            # Preserve the previous sizes to avoid clobbering both with the
            # same value (which would make order-book imbalance always 0).
            prev = self._last.get(asset_id)
            prev_bid_size = prev.get("bid_size") if prev else None
            prev_ask_size = prev.get("ask_size") if prev else None
            record = self._update_state(asset_id, best_bid, best_ask, prev_bid_size, prev_ask_size)
            self._emit(asset_id, record)

    def _handle_best_bid_ask(self, payload: Dict[str, Any]) -> None:
        """Handle direct best_bid_ask event."""
        asset_id = payload.get("asset_id", "")
        if not asset_id:
            return
        bb = payload.get("best_bid")
        ba = payload.get("best_ask")
        best_bid = float(bb) if bb is not None else None
        best_ask = float(ba) if ba is not None else None
        record = self._update_state(asset_id, best_bid, best_ask)
        self._emit(asset_id, record)

    def _handle_last_trade(self, payload: Dict[str, Any]) -> None:
        """Handle last_trade_price event."""
        asset_id = payload.get("asset_id", "")
        if not asset_id:
            return
        price = payload.get("price")
        ltp = float(price) if price is not None else None
        prev = self._last.get(asset_id)
        if prev:
            record = self._update_state(
                asset_id,
                prev.get("best_bid"),
                prev.get("best_ask"),
                prev.get("bid_size"),
                prev.get("ask_size"),
                ltp,
            )
            self._emit(asset_id, record)

    def _emit(self, asset_id: str, record: Dict[str, Any]) -> None:
        """Call the on_book callback if registered."""
        if self.on_book:
            try:
                self.on_book(
                    asset_id,
                    record.get("best_bid"),
                    record.get("best_ask"),
                    record.get("bid_size"),
                    record.get("ask_size"),
                    record.get("last_trade_price"),
                    record["timestamp"],
                    record.get("stale", False),
                )
            except Exception:
                pass  # Don't let callback failures kill the collector

    # ---- WebSocket lifecycle ------------------------------------------------

    def _subscribe_message(self, asset_ids: list[str]) -> Dict[str, Any]:
        return {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }

    def _update_subscription_message(self, asset_ids: list[str]) -> Dict[str, Any]:
        return {
            "operation": "subscribe",
            "assets_ids": asset_ids,
        }

    async def _consume(self) -> None:
        """Main WebSocket connection + consume loop."""
        while self._running:
            try:
                async with websockets.connect(
                    self.WS_URL,
                    ping_interval=None,  # We handle pings manually per docs
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    # Subscribe to current assets
                    async with self._lock:
                        if self._assets_ids:
                            await ws.send(json.dumps(self._subscribe_message(self._assets_ids)))

                    # Start ping task
                    ping_task = asyncio.create_task(self._pinger(ws))

                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            try:
                                msg = json.loads(raw)
                                await self._handle_message(msg)
                            except json.JSONDecodeError:
                                continue
                            except Exception:
                                continue
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass

            except ConnectionClosed as exc:
                if self._running and self.on_disconnect:
                    try:
                        self.on_disconnect(list(self._assets_ids), datetime.now(timezone.utc), f"book_disconnect:{type(exc).__name__}")
                    except Exception:
                        logger.exception("Book disconnect callback failed")
            except Exception as exc:
                if self._running and self.on_disconnect:
                    try:
                        self.on_disconnect(list(self._assets_ids), datetime.now(timezone.utc), f"book_disconnect:{type(exc).__name__}")
                    except Exception:
                        logger.exception("Book disconnect callback failed")

            self._ws = None
            if self._running:
                await asyncio.sleep(3)

    async def _pinger(self, ws) -> None:
        """Send ping {} every PING_INTERVAL seconds."""
        while self._running:
            try:
                await ws.send("{}")
            except Exception:
                break
            await asyncio.sleep(self.PING_INTERVAL)

    def _handle_resolution(self, msg: Dict[str, Any]) -> None:
        """Handle market_resolved event."""
        asset_id = msg.get("winning_asset_id", "")
        outcome = msg.get("winning_outcome")
        if asset_id and outcome in ("Yes", "No") and self.on_resolution:
            try:
                self.on_resolution(asset_id, outcome)
            except Exception:
                pass

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        """Route incoming messages by event_type."""
        event_type = msg.get("event_type")
        if event_type == "book":
            self._handle_book(msg)
        elif event_type == "price_change":
            self._handle_price_change(msg)
        elif event_type == "best_bid_ask":
            self._handle_best_bid_ask(msg)
        elif event_type == "last_trade_price":
            self._handle_last_trade(msg)
        elif event_type == "market_resolved":
            self._handle_resolution(msg)
            logger.info("Resolution event: %s", msg)
        elif event_type in ("tick_size_change", "new_market", "pong", None):
            pass
        else:
            logger.debug("Unknown event type=%r msg=%r", event_type, msg)

    async def _carry_forward_loop(self) -> None:
        """Background task: emit stale carry-forward records for silent tokens."""
        while self._running:
            await asyncio.sleep(self.carry_timeout_sec)
            if not self._running:
                break
            now = datetime.now(timezone.utc)
            for asset_id in list(self._assets_ids):
                last_seen = self._last_seen.get(asset_id)
                if last_seen and (now - last_seen).total_seconds() > self.carry_timeout_sec:
                    record = self._get_stale_record(asset_id)
                    self._emit(asset_id, record)

    # ---- public API ----------------------------------------------------------

    def start(self, asset_ids: list[str]) -> None:
        self._running = True
        self._assets_ids = list(asset_ids)
        self._task = asyncio.create_task(self._consume())
        self._carry_task = asyncio.create_task(self._carry_forward_loop())

    async def update_assets(self, asset_ids: list[str]) -> None:
        """Update the subscription list.

        NOTE: The Polymarket market WS protocol uses additive-only
        subscription via {"operation": "subscribe", ...}. There is no
        unsubscribe mechanism. When tokens need to be removed, we force
        a reconnect so the new connection starts with a clean subscription.
        """
        async with self._lock:
            old_ids = set(self._assets_ids)
            new_ids = set(asset_ids)
            self._assets_ids = list(asset_ids)

            # If tokens were removed, force reconnect (additive-only protocol)
            if old_ids and not new_ids.issuperset(old_ids):
                if self._ws:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                return

            if self._ws and self._ws.state == State.OPEN:
                try:
                    await self._ws.send(json.dumps(self._update_subscription_message(self._assets_ids)))
                except Exception:
                    pass

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._carry_task:
            self._carry_task.cancel()
            try:
                await self._carry_task
            except asyncio.CancelledError:
                pass
