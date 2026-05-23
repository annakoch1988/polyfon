"""Binance spot price collection via WebSocket."""
import asyncio
import json
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

import websockets
from tenacity import retry, stop_after_attempt, wait_exponential

from polyfon.config import settings


class BinanceSpotCollector:
    """Collect spot prices from Binance WebSocket ticker streams.

    Uses the lightweight ticker stream:
        wss://stream.binance.com:9443/ws/btcusdt@ticker/ethusdt@ticker
    """

    def __init__(
        self,
        coins: Optional[list[str]] = None,
        on_price: Optional[Callable[[str, float, datetime], None]] = None,
    ):
        self.coins = [c.upper() for c in (coins or settings.coin_list)]
        self.on_price = on_price
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._latest: Dict[str, float] = {}

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
                                if self.on_price:
                                    self.on_price(symbol, price, ts)
                        except Exception:
                            continue
            except Exception:
                await asyncio.sleep(5)

    def latest(self, coin: str) -> Optional[float]:
        return self._latest.get(coin.upper())

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
