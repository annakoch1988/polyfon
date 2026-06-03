"""Clock providers for boundary alignment independent of system clock drift.

Provides a unified ``now_utc()`` that can be sourced from:
- system: local machine clock (default)
- binance: Binance server time (/api/v3/time) with periodic resync

The Binance-based clock samples once at start and then every
``time_sync_interval_sec``; between samples it advances using
``time.monotonic()`` to avoid dependency on wall clock adjustments.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx


class BaseClock:
    async def start(self) -> None:  # pragma: no cover
        pass

    async def stop(self) -> None:  # pragma: no cover
        pass

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class SystemClock(BaseClock):
    pass


class BinanceClock(BaseClock):
    def __init__(self, sync_interval_sec: int = 60, ws_url: str = "https://api.binance.com"):
        self.sync_interval_sec = max(10, int(sync_interval_sec))
        self.api_base = ws_url.rstrip("/")
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._base_server_ts: Optional[float] = None  # seconds since epoch (float)
        self._base_mono: Optional[float] = None

    async def start(self) -> None:
        self._running = True
        # Initial sync (blocking)
        await self._sync_once()
        # Background resync
        self._task = asyncio.create_task(self._resync_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def now_utc(self) -> datetime:
        if self._base_server_ts is None or self._base_mono is None:
            return datetime.now(timezone.utc)
        dt = self._base_server_ts + (time.monotonic() - self._base_mono)
        return datetime.fromtimestamp(dt, tz=timezone.utc)

    async def _resync_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.sync_interval_sec)
            if not self._running:
                break
            try:
                await self._sync_once()
            except Exception:
                # Ignore and try again on next tick
                pass

    async def _sync_once(self) -> None:
        # Binance REST: GET /api/v3/time returns {"serverTime": 1717419999229}
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.api_base}/api/v3/time")
            resp.raise_for_status()
            data = resp.json()
            ms = float(data.get("serverTime"))
            server_ts = ms / 1000.0
        self._base_server_ts = server_ts
        self._base_mono = time.monotonic()


def make_clock(source: str, sync_interval_sec: int = 60) -> BaseClock:
    s = (source or "system").lower()
    if s == "binance":
        return BinanceClock(sync_interval_sec=sync_interval_sec)
    return SystemClock()
