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
        """Fetch the single page of events for this series.

        The Gamma API caps responses at 100 events and pagination (skip)
        wraps around instead of returning new pages.  We fetch one page
        and return every event whose end time is within a sane lookahead
        window (now – 1 hour  →  now + settings.discovery_horizon_minutes),
        so the caller can discover the next few windows without spamming
        the console for hours ahead.
        """
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
            return []
        if not isinstance(page, list):
            return []

        from polyfon.config import settings
        now = datetime.now(timezone.utc)
        horizon = timedelta(minutes=max(5, int(settings.discovery_horizon_minutes)))
        results: list = []
        for ev in page:
            # Prefer end time from slug; fallback to parsing endDate as ET
            end_utc = self._end_from_slug((ev.get("markets") or [{}])[0].get("slug") or ev.get("slug", ""))
            if end_utc is None:
                end_str = ev.get("endDate")
                if not end_str:
                    continue
                try:
                    # Polymarket endDate strings are ET clock times with trailing 'Z'.
                    # Interpret as ET then convert to UTC.
                    et_naive = datetime.fromisoformat(end_str.replace("Z", ""))
                    et_aware = et_naive.replace(tzinfo=ET_TZ)
                    end_utc = et_aware.astimezone(timezone.utc).replace(tzinfo=None)
                except Exception:
                    continue
            if now - timedelta(hours=1) <= end_utc <= now + horizon:
                results.append(ev)
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

    @staticmethod
    def _parse_iso_utc(value: Any) -> Optional[datetime]:
        """Parse a Polymarket/Gamma ISO datetime as naive UTC.

        Gamma timing fields such as ``eventStartTime`` and ``endDate`` are UTC
        instants with a trailing ``Z``. Store all DB-facing timestamps as naive
        UTC to match the rest of the project.
        """
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
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

        # Timing: prefer explicit UTC fields from Gamma.
        # For recurring 5-minute markets these reflect the true UTC window
        # boundaries and are more reliable than reverse-engineering the slug.
        event_start = event.get("eventStartTime") or market0.get("eventStartTime")
        start_utc = self._parse_iso_utc(event_start)

        end_str = market0.get("endDate") or event.get("endDate")
        end_utc = self._parse_iso_utc(end_str)

        # Fallback for older payloads that may omit explicit timing fields.
        if end_utc is None:
            slug_end = self._end_from_slug(slug)
            if slug_end is not None:
                end_utc = slug_end.replace(tzinfo=None)

        if start_utc is None and end_utc is not None:
            start_utc = end_utc - timedelta(minutes=5)

        if start_utc is None or end_utc is None:
            return None

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
        """Keep events whose window hasn't closed yet (+ upcoming ones soon).

        The upper bound is governed by settings.discovery_horizon_minutes.
        """
        # Compute end time as in _series_events(): prefer slug, else ET endDate
        slug = None
        mkt = event.get("markets")
        if isinstance(mkt, list) and mkt:
            slug = mkt[0].get("slug")
        if not slug:
            slug = event.get("slug")
        end_utc = PolymarketDiscovery._end_from_slug(slug) if slug else None
        if end_utc is None:
            end_str = event.get("endDate")
            if end_str:
                try:
                    et_naive = datetime.fromisoformat(end_str.replace("Z", ""))
                    et_aware = et_naive.replace(tzinfo=ET_TZ)
                    end_utc = et_aware.astimezone(timezone.utc)
                except Exception:
                    end_utc = None
        from polyfon.config import settings
        now = datetime.now(timezone.utc)
        horizon = timedelta(minutes=max(5, int(settings.discovery_horizon_minutes)))
        return now - timedelta(minutes=5) <= end_utc <= now + horizon

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

    async def discover_crypto_5min(self, coins: Optional[List[str]] = None, lookahead: int = 6) -> List[Dict[str, Any]]:
        """Discover active + upcoming 5-min crypto markets.

        Generates window slugs from ET clock boundaries and queries Gamma
        API individually by slug (bypasses broken _series_events pagination).

        Returns one dict per event (not per token_id).
        """
        all_results: List[Dict[str, Any]] = []

        coin_list = coins or ["BTC", "ETH"]
        now_et = datetime.now(timezone.utc).astimezone(ET_TZ)

        # Anchor discovery on the current ET 5-minute slot boundary so the
        # currently-opening window is included as the first candidate.
        slot_boundary = now_et.replace(
            minute=(now_et.minute // 5) * 5,
            second=0,
            microsecond=0,
        )

        for coin in coin_list:
            prefix = {"BTC": "btc-updown-5m", "ETH": "eth-updown-5m"}.get(
                coin, f"{coin.lower()}-updown-5m"
            )

            for i in range(lookahead):
                # Window i spans [slot_boundary + i*5m, slot_boundary + (i+1)*5m].
                window_end_et = slot_boundary + timedelta(minutes=(i + 1) * 5)
                end_ts = int(window_end_et.timestamp())
                slug = f"{prefix}-{end_ts}"

                event = await self._fetch_event_by_slug(slug)
                if event is None:
                    continue
                normalised = self._normalise(event)
                if normalised:
                    all_results.append(normalised)

        return all_results

    async def _fetch_event_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch a single Gamma event by slug."""
        try:
            data = await self._get("/events", params={"slug": slug})
            if isinstance(data, list) and data:
                return data[0]
        except Exception:
            pass
        return None
