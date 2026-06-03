"""Polymarket order book collection via WebSocket.

Subscribes to the public market WebSocket for real-time orderbook,
price change, and best_bid_ask events. Uses carry-forward when no
update arrives within a timeout window.

WebSocket endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
"""
import asyncio
import json
import logging
import traceback
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
        self._expected_close = False
        self._unknown_messages_logged = 0

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
            if self._unknown_messages_logged < 10:
                logger.warning("Book WS book payload missing asset_id payload=%r", payload)
                self._unknown_messages_logged += 1
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
        changes = payload.get("price_changes", [])
        if not isinstance(changes, list):
            if self._unknown_messages_logged < 10:
                logger.warning("Book WS price_change payload has non-list price_changes payload=%r", payload)
                self._unknown_messages_logged += 1
            return
        for change in changes:
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
            if self._unknown_messages_logged < 10:
                logger.warning("Book WS best_bid_ask payload missing asset_id payload=%r", payload)
                self._unknown_messages_logged += 1
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
            if self._unknown_messages_logged < 10:
                logger.warning("Book WS last_trade_price payload missing asset_id payload=%r", payload)
                self._unknown_messages_logged += 1
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
                logger.exception("Book WS on_book callback failed for asset_id=%s record=%r", asset_id, record)

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
        """Main WebSocket connection + consume loop.

        Uses ``async for`` (reconnect iterator) instead of ``async with``,
        so the websockets library itself handles reconnection with
        exponential backoff on transient failures.

        The custom ``_pinger`` task and ``on_disconnect`` callback are
        kept for compatibility with the orchestrator's invalidation logic.
        """
        from websockets.asyncio.client import connect as ws_connect

        while self._running:
            try:
                async for ws in ws_connect(
                    self.WS_URL,
                    ping_interval=None,
                    close_timeout=5,
                    open_timeout=10,
                ):
                    self._ws = ws
                    logger.debug("Book WS: connected, subscribing %d assets", len(self._assets_ids))
                    async with self._lock:
                        if self._assets_ids:
                            await ws.send(json.dumps(self._subscribe_message(self._assets_ids)))

                    ping_task = asyncio.create_task(self._pinger(ws))
                    logger.debug("Book WS: starting consume loop")
                    try:
                        while self._running:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                            except asyncio.TimeoutError:
                                continue
                            try:
                                msg = json.loads(raw)
                                await self._handle_message(msg)
                            except json.JSONDecodeError:
                                continue
                            except Exception:
                                continue
                    except ConnectionClosed as exc:
                        logger.warning("Book WS ConnectionClosed: %s", exc)
                        expected_close = self._expected_close
                        self._expected_close = False
                        if self._running and self.on_disconnect and not expected_close:
                            try:
                                self.on_disconnect(list(self._assets_ids), datetime.now(timezone.utc), f"book_disconnect:{type(exc).__name__}")
                            except Exception:
                                logger.exception("Book disconnect callback failed")
                    except Exception as exc:
                        logger.warning("Book WS exception in recv: %s %s", type(exc).__name__, exc)
                        if self._running and self.on_disconnect:
                            try:
                                self.on_disconnect(list(self._assets_ids), datetime.now(timezone.utc), f"book_disconnect:{type(exc).__name__}")
                            except Exception:
                                logger.exception("Book disconnect callback failed")
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                    self._ws = None
                    if not self._running:
                        break
            except asyncio.TimeoutError:
                logger.warning("Book WS connect TimeoutError:\n%s", traceback.format_exc())
                if self._running:
                    await asyncio.sleep(3)
            except Exception as exc:
                logger.warning("Book WS connect exception: %s %s\n%s", type(exc).__name__, exc, traceback.format_exc())
                if self._running:
                    await asyncio.sleep(3)
            self._ws = None
            if not self._running:
                break

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

    async def _handle_message(self, msg: Any) -> None:
        """Route incoming messages by event_type.

        Polymarket may emit either a single event dict or a list of event dicts.
        """
        if isinstance(msg, list):
            for item in msg:
                await self._handle_message(item)
            return

        if not isinstance(msg, dict):
            if self._unknown_messages_logged < 5:
                logger.warning("Book WS unsupported message type=%s msg=%r", type(msg).__name__, msg)
                self._unknown_messages_logged += 1
            return

        payload = msg.get("message") if isinstance(msg.get("message"), dict) else msg

        # Some Polymarket frames may wrap the actual event in a nested list
        # under "message" or send a list-shaped payload directly.
        if isinstance(payload, list):
            for item in payload:
                await self._handle_message(item)
            return

        event_type = payload.get("event_type") or payload.get("type")
        if event_type == "book":
            self._handle_book(payload)
        elif event_type == "price_change":
            self._handle_price_change(payload)
        elif event_type == "best_bid_ask":
            self._handle_best_bid_ask(payload)
        elif event_type == "last_trade_price":
            self._handle_last_trade(payload)
        elif event_type == "market_resolved":
            self._handle_resolution(payload)
            logger.info("Resolution event: %s", payload)
        elif event_type in ("tick_size_change", "new_market", "pong", None):
            if event_type is None and self._unknown_messages_logged < 5:
                logger.warning("Book WS message missing event_type keys=%s msg=%r", sorted(payload.keys()), payload)
                self._unknown_messages_logged += 1
        else:
            if self._unknown_messages_logged < 5:
                logger.warning("Book WS unknown event type=%r msg=%r", event_type, payload)
                self._unknown_messages_logged += 1

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
        logger.info("Book WS start requested for %d assets", len(asset_ids))
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
            logger.info(
                "Book WS update_assets old=%d new=%d removed=%d added=%d",
                len(old_ids),
                len(new_ids),
                len(old_ids - new_ids),
                len(new_ids - old_ids),
            )

            # If tokens were removed, force reconnect (additive-only protocol)
            if old_ids and not new_ids.issuperset(old_ids):
                if self._ws:
                    try:
                        self._expected_close = True
                        await self._ws.close()
                    except Exception:
                        self._expected_close = False
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
