"""Discover active 5-minute crypto prediction markets on Polymarket.

Strategy
--------
1. Hit the Gamma ``/series`` endpoint to find recurring crypto 5-minute
   series (e.g. ``btc-up-or-down-5m``).
2. For each series, query ``/events?series_slug=<slug>&closed=false`` to get
   non-closed (active/future) markets.
3. Extract the per-outcome token IDs from the nested ``markets`` array so
   the WebSocket book collector can subscribe to them.

Why not the CLOB /markets endpoint?
Polymarket's CLOB ``/markets?limit=N`` paginated endpoint starts from the
oldest markets first. Currently it serves ~50k historical sports markets
before ever reaching today's 5-min crypto events, making it unusable for
real-time discovery.
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

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
        """Fetch metadata for a single series slug."""
        try:
            data = await self._get("/series", params={"slug": series_slug})
            if isinstance(data, list) and data:
                return data[0]
        except Exception:
            pass
        return None

    async def _series_events(
        self, series_slug: str, closed: bool = False, limit: int = 200, skip: int = 0
    ) -> List[Dict[str, Any]]:
        """Fetch events (markets) belonging to a series."""
        try:
            data = await self._get(
                "/events",
                params={
                    "series_slug": series_slug,
                    "closed": closed,
                    "limit": limit,
                    "skip": skip,
                },
            )
            return data if isinstance(data, list) else []
        except Exception:
            return []

    # --- normalisation ------------------------------------------------------

    @staticmethod
    def _parse_clob_token_ids(raw: Any) -> List[str]:
        """Handle stringly-encoded JSON arrays (``"[\"id1\", \"id2\"]"``)."""
        if isinstance(raw, list):
            return [str(t) for t in raw]
        if isinstance(raw, str) and raw.startswith("["):
            try:
                return [str(t) for t in _json.loads(raw)]
            except Exception:
                pass
        return []

    @staticmethod
    def _normalise(event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Convert a Gamma event → one dict per token/outcome.

        Events carry a ``markets`` list; each market element carries
        ``clobTokenIds`` and the usual fee / tick-size metadata.
        """
        event_slug = event.get("slug", "")
        question = event.get("title") or event.get("question", "Unknown")
        desc = event.get("description", "")
        condition_id = event.get("conditionId") or event.get("condition_id", "")
        neg_risk = event.get("negRisk", False)

        markets = event.get("markets")
        if not isinstance(markets, list) or not markets:
            return []

        # Just take the first market entry – it has clobTokenIds, feeSchedule, etc.
        market0 = markets[0]
        slug = market0.get("slug") or event_slug
        tick_size = float(market0.get("orderPriceMinTickSize", 0.01))
        token_ids = PolymarketDiscovery._parse_clob_token_ids(
            market0.get("clobTokenIds")
        )

        fee = 0.07
        fee_sched = market0.get("feeSchedule") or {}
        if isinstance(fee_sched, dict) and "rate" in fee_sched:
            fee = float(fee_sched["rate"])

        # Infer underlying from the event title
        title_lower = question.lower()
        if "bitcoin" in title_lower or "btc" in title_lower:
            underlying = "BTC"
        elif "ethereum" in title_lower or "eth" in title_lower:
            underlying = "ETH"
        else:
            underlying = "BTC"

        # Extract strike from title if it looks like an above/below market.
        # "Up or Down" markets have no fixed strike, so leave it None.
        import re
        if " up or down" in title_lower:
            strike = None
        else:
            strike_match = re.search(r"\$?([0-9,]+(?:\.[0-9]+)?)", question)
            strike = float(strike_match.group(1).replace(",", "")) if strike_match else None

        results: List[Dict[str, Any]] = []
        for tid in token_ids:
            results.append(
                {
                    "token_id": tid,
                    "asset_id": tid,
                    "condition_id": condition_id,
                    "conditionId": condition_id,
                    "slug": slug,
                    "title": question,
                    "description": desc,
                    "category": "crypto",
                    "fee_rate": fee,
                    "tick_size": tick_size,
                    "neg_risk": neg_risk,
                    "end_date_iso": event.get("endDate"),
                    "resolution_time": event.get("endDate"),
                    "negRisk": neg_risk,
                    "raw_tags": event.get("tags", []),
                    "active": event.get("active", False),
                    "closed": event.get("closed", True),
                    "underlying": underlying,
                    "strike": strike,
                }
            )
        return results

    @staticmethod
    def _is_relevant_now(event: Dict[str, Any]) -> bool:
        """Return True for events whose time window is close to the current time.

        Polymarket leaves old 5-min markets with ``closed=false`` for weeks
        until they are manually resolved.  We filter those out by only keeping
        events whose end time is within [-5min, +15min] of now.
        """
        from datetime import datetime, timezone, timedelta

        end_str = event.get("endDate")
        if not end_str:
            return False
        try:
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except Exception:
            return False

        now = datetime.now(timezone.utc)
        return (now - timedelta(minutes=5)) <= end <= (now + timedelta(minutes=15))

    # --- public API ----------------------------------------------------------

    async def discover_crypto_5min(self, coins: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Discover 5-minute crypto markets relevant right now.

        Only returns events whose 5-minute window overlaps with the current
        time (± a small buffer) so the collector doesn't drown in hundreds
        of stale ``closed=false`` markets.

        Note: the Gamma ``/events`` endpoint ignores ``skip`` for this
        series, so we fetch exactly **one** page and filter client-side.
        """
        from datetime import datetime, timezone

        all_results: List[Dict[str, Any]] = []

        slug_map = {"BTC": "btc-up-or-down-5m", "ETH": "eth-up-or-down-5m"}
        slugs = [slug_map[c] for c in (coins or ["BTC", "ETH"]) if c in slug_map]

        for series_slug in slugs:
            series = await self._series_info(series_slug)
            if not series:
                continue

            # Single fetch — skip doesn't work for this endpoint
            events = await self._series_events(
                series_slug, closed=False, limit=1000, skip=0
            )
            if not events:
                continue

            for event in events:
                if self._is_relevant_now(event):
                    all_results.extend(self._normalise(event))

        return all_results
