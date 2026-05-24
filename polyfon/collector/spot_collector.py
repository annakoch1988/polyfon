"""Binance spot price collection via WebSocket."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

import websockets

from polyfon.config import settings

logger = logging.getLogger(__name__)


class BinanceSpotCollector:
    """Collect spot prices from Binance WebSocket ticker streams.

    Uses the lightweight ticker stream:
        wss://stream.binance.com:9443/ws/btcusdt@ticker/ethusdt@ticker
    """

    def __init__(
        self,
        coins: Optional[list[str]] = None,
        on_price: Optional[Callable[[str, float, datetime], None]] = None,
        on_disconnect: Optional[Callable[[list[str], datetime, str], None]] = None,
    ):
        self.coins = [c.upper() for c in (coins or settings.coin_list)]
        self.on_price = on_price
        self.on_disconnect = on_disconnect
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._latest: Dict[str, float] = {}
        self._last_message_at: Dict[str, datetime] = {}
        self._watchdog_task: Optional[asyncio.Task] = None

    def _streams(self) -> str:
        streams = "/".join(f"{c.lower()}usdt@ticker" for c in self.coins)
        return f"{settings.binance_ws_url}/{streams}"

    async def _consume(self) -> None:
        uri = self._streams()
        while self._running:
            try:
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            # Handle both single messages and combined stream messages
                            payload = data.get("data", data)
                            symbol = (payload.get("s") or "").replace("USDT", "")
                            price = float(payload.get("c", 0))
                            if symbol and price > 0:
                                ts = datetime.now(timezone.utc)
                                self._latest[symbol] = price
                                self._last_message_at[symbol] = ts
                                if self.on_price:
                                    self.on_price(symbol, price, ts)
                        except Exception:
                            continue
            except Exception as exc:
                if self._running and self.on_disconnect:
                    try:
                        self.on_disconnect(self.coins, datetime.now(timezone.utc), f"spot_disconnect:{type(exc).__name__}")
                    except Exception:
                        logger.exception("Spot disconnect callback failed")
                await asyncio.sleep(5)

    async def _watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(0.25)
            if not self._running:
                break
            now = datetime.now(timezone.utc)
            stale = [
                coin for coin in self.coins
                if (last := self._last_message_at.get(coin)) is not None
                and (now - last).total_seconds() > settings.binance_silence_threshold_sec
            ]
            if stale and self.on_disconnect:
                try:
                    self.on_disconnect(stale, now, "spot_silence_timeout")
                except Exception:
                    logger.exception("Spot silence callback failed")
                for coin in stale:
                    self._last_message_at[coin] = now

    def latest(self, coin: str) -> Optional[float]:
        return self._latest.get(coin.upper())

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._consume())
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
