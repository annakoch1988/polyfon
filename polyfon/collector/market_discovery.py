"""Discover active 5-minute crypto prediction markets on Polymarket.

Returns one record per event (e.g. "BTC Up or Down, 9:05-9:10PM ET")
with both UP/DOWN token IDs, rather than one record per token.
"""
from __future__ import annotations

import json as _json
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")
GAMMA_API_URL = "https://gamma-api.polymarket.com"

_SERIES_SLUGS = [
    "btc-up-or-down-5m",
    "eth-up-or-down-5m",
]


class PolymarketDiscovery:
    """Gamma API–based discovery for recurring 5-min crypto markets."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or GAMMA_API_URL

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            resp.raise_for_status()
            return resp.json()

    async def _series_info(self, series_slug: str) -> Optional[Dict[str, Any]]:
        try:
            data = await self._get("/series", params={"slug": series_slug})
            if isinstance(data, list) and data:
                return data[0]
        except Exception:
            pass
        return None

    async def _series_events(
        self, series_slug: str, closed: bool = False, limit: int = 100, skip: int = 0
    ) -> List[Dict[str, Any]]:
        """Fetch events, paginating until we find relevant (recent) ones.

        The Gamma API can return thousands of stale events first, so we
        keep requesting pages until we either find events that pass
        ``_is_relevant_now`` or exhaust the result set.
        """
        results: list = []
        now = datetime.now(timezone.utc)
        while True:
            try:
                page = await self._get(
                    "/events",
                    params={
                        "series_slug": series_slug,
                        "closed": closed,
                        "limit": limit,
                        "skip": skip,
                    },
                )
            except Exception:
                return results
            if not isinstance(page, list) or not page:
                return results
            for ev in page:
                end_str = ev.get("endDate")
                if end_str:
                    try:
                        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if end >= now - timedelta(hours=1):
                        results.append(ev)
            if any(self._is_relevant_now(ev) for ev in page):
                return results
            skip += limit
            if skip > 2000:
                return results

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _parse_clob_token_ids(raw: Any) -> List[str]:
        if isinstance(raw, list):
            return [str(t) for t in raw]
        if isinstance(raw, str) and raw.startswith("["):
            try:
                return [str(t) for t in _json.loads(raw)]
            except Exception:
                pass
        return []

    @staticmethod
    def _infer_underlying(title: str) -> str:
        tl = title.lower()
        if "bitcoin" in tl or "btc" in tl:
            return "BTC"
        if "ethereum" in tl or "eth" in tl:
            return "ETH"
        return "BTC"

    @staticmethod
    def _end_from_slug(slug: str) -> Optional[datetime]:
        m = re.search(r"-([0-9]{10})$", slug)
        if m:
            return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        return None

    # --- normalisation --------------------------------------------------------

    def _normalise(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert a Gamma event → one dict per event (not per token)."""
        markets = event.get("markets")
        if not isinstance(markets, list) or not markets:
            return None

        market0 = markets[0]
        token_ids = self._parse_clob_token_ids(market0.get("clobTokenIds"))
        if not token_ids:
            return None

        title = event.get("title") or event.get("question", "Unknown")
        slug = market0.get("slug") or event.get("slug", "")

        condition_id = event.get("conditionId") or event.get("condition_id", "")

        fee = 0.07
        fee_sched = market0.get("feeSchedule") or {}
        if isinstance(fee_sched, dict) and "rate" in fee_sched:
            fee = float(fee_sched["rate"])

        tick_size = float(market0.get("orderPriceMinTickSize", 0.01))

        # Parse end time — Polymarket ET-based ISO string → UTC internally
        end_str = event.get("endDate")
        end_utc: Optional[datetime] = None
        if end_str:
            try:
                dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                # Convert to UTC for storage, keep the ET boundary semantics
                end_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
        if end_utc is None:
            end_utc = self._end_from_slug(slug)

        if end_utc is None:
            return None

        start_utc = end_utc - timedelta(minutes=5)

        return {
            "slug": slug,
            "title": title,
            "underlying": self._infer_underlying(title),
            "start_utc": start_utc,
            "end_utc": end_utc,
            "up_token_id": token_ids[0],
            "down_token_id": token_ids[1] if len(token_ids) > 1 else token_ids[0],
            "condition_id": condition_id,
            "fee_rate": fee,
            "tick_size": tick_size,
        }

    @staticmethod
    def _is_relevant_now(event: Dict[str, Any]) -> bool:
        """Keep events whose window hasn't closed yet (+ keep future ones)."""
        end_str = event.get("endDate")
        if not end_str:
            return False
        try:
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except Exception:
            return False
        now = datetime.now(timezone.utc)
        return now - timedelta(minutes=5) <= end <= now + timedelta(minutes=30)

    async def fetch_resolution(self, slug: str) -> Optional[str]:
        """Return ``"Yes"`` or ``"No"`` if the event has been resolved, else ``None``."""
        try:
            data = await self._get("/events", params={"slug": slug})
            if not isinstance(data, list) or not data:
                return None
            event = data[0]
            if not event.get("closed"):
                return None
            markets = event.get("markets")
            if not isinstance(markets, list) or not markets:
                return None
            m = markets[0]

            outcome = m.get("outcome")
            if outcome in ("Yes", "No"):
                return outcome

            # Gamma API stringifies these arrays for automated markets
            outcomes_raw = m.get("outcomes")
            prices_raw = m.get("outcomePrices")
            if outcomes_raw and prices_raw:
                if isinstance(outcomes_raw, str):
                    outcomes = _json.loads(outcomes_raw)
                else:
                    outcomes = outcomes_raw
                if isinstance(prices_raw, str):
                    prices = _json.loads(prices_raw)
                else:
                    prices = prices_raw
                if isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices):
                    for o, p in zip(outcomes, prices):
                        if str(p) == "1":
                            return "Yes" if str(o).lower() == "up" else "No"
            return None
        except Exception:
            return None

    # --- public API -----------------------------------------------------------

    async def discover_crypto_5min(self, coins: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Discover active + upcoming 5-min crypto markets.

        Returns one dict per event (not per token_id).
        """
        all_results: List[Dict[str, Any]] = []

        slug_map = {"BTC": "btc-up-or-down-5m", "ETH": "eth-up-or-down-5m"}
        slugs = [slug_map[c] for c in (coins or ["BTC", "ETH"]) if c in slug_map]

        for series_slug in slugs:
            series = await self._series_info(series_slug)
            if not series:
                continue

            events = await self._series_events(
                series_slug, closed=False, limit=1000, skip=0
            )
            if not events:
                continue

            for event in events:
                if self._is_relevant_now(event):
                    normalised = self._normalise(event)
                    if normalised:
                        all_results.append(normalised)

        return all_results
