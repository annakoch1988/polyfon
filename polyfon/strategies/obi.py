"""Strategy: Order Book Imbalance (OBI)."""
from datetime import timedelta
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class OBIStrategy(BaseStrategy):
    number = 7
    """Order Book Imbalance.

    Exploits order-book micro-structure by measuring the ratio of bid to
    ask sizes at the top of the book for the UP token.  A strong
    imbalance toward the bid side indicates aggressive buy pressure
    (market participants are lifting offers), while a strong imbalance
    toward the ask side indicates selling pressure.

    The strategy is symmetric: it can enter YES (when buy pressure is
    dominant) or NO (when sell pressure is dominant).

    Imbalance is computed on the UP token's book:
        imbalance = (bid_size - ask_size) / (bid_size + ask_size)

    Range: [-1, +1].  Positive → buy pressure → BUY_YES.
    Negative → sell pressure → BUY_NO.

    Parameters (empirically discoverable):
        theta_entry:  Min |imbalance| to consider (default 0.10).
                      Lower → more signals at weaker pressure.
        theta_sat:    |imbalance| at which confidence saturates (default 0.30).
        tau_max:      Latest entry before resolution (default 120s).
        tau_min:      Earliest entry before resolution (default 15s).
        q_max:        Position size.  For *market* class = USDC to spend;
                      for *limit* class = number of shares.
        theta_edge:   Minimum expected edge per share to trade.
        order_class:  'market' (default) or 'limit'.
        time_in_force: 'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
                       Market -> FOK or FAK.  Limit -> GTC or GTD.
        fee_rate:     Market taker fee rate.
    """

    name = "OBI"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        theta_entry: float = 0.10,
        theta_sat: float = 0.30,
        tau_max: float = 120.0,
        tau_min: float = 15.0,
        replay_cadence_seconds: float = 1.0,
        q_max: float = 1.0,
        theta_edge: float = 0.01,
        order_class: str = "market",
        time_in_force: str = "FOK",
        fee_rate: float = 0.07,
    ):
        if order_class not in ("market", "limit"):
            raise ValueError(f"order_class must be 'market' or 'limit', got {order_class!r}")
        valid_tif = self._MARKET_TIF if order_class == "market" else self._LIMIT_TIF
        if time_in_force not in valid_tif:
            raise ValueError(
                f"time_in_force {time_in_force!r} invalid for {order_class} orders. "
                f"Use {sorted(valid_tif)}"
            )
        self.theta_entry = theta_entry
        self.theta_sat = theta_sat
        self.tau_max = tau_max
        self.tau_min = tau_min
        self.replay_cadence_seconds = replay_cadence_seconds
        self.q_max = q_max
        self.theta_edge = theta_edge
        self.order_class = order_class
        self.time_in_force = time_in_force
        self.fee_rate = fee_rate

    def _resolve_order(self, price: float) -> tuple[float, float] | None:
        """Return (shares, notional_usdc) or None if constraints fail."""
        if self.order_class == "market":
            usdc = self.q_max
            if usdc < 1.0:
                return None
            shares = usdc / price
        else:
            shares = self.q_max
            if shares < 5 or shares * price < 1.0:
                return None
        return shares, shares * price

    def on_tick(self, window: Any, context: Context) -> Optional[Signal]:
        if context.spot_price is None:
            return None
        if context.tau_seconds is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        # Compute imbalance from the UP token's book.
        # Fall back to DOWN token if UP sizes are unavailable.
        bid_size = context.up_bid_size
        ask_size = context.up_ask_size
        if bid_size is None or ask_size is None or (bid_size + ask_size) <= 0:
            bid_size = context.down_bid_size
            ask_size = context.down_ask_size
            if bid_size is None or ask_size is None or (bid_size + ask_size) <= 0:
                return None

        imbalance = (bid_size - ask_size) / (bid_size + ask_size)

        if abs(imbalance) < self.theta_entry:
            return None

        if imbalance > self.theta_entry and context.up_best_ask is not None:
            price = context.up_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(imbalance / self.theta_sat, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "imbalance": imbalance,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max, "notional": notional,
                    },
                )

        if imbalance < -self.theta_entry and context.down_best_ask is not None:
            price = context.down_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(-imbalance / self.theta_sat, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "imbalance": imbalance,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max, "notional": notional,
                    },
                )

        return None

    def build_replay_plan(self, window: Any) -> ReplayPlan:
        start = window.end_et - timedelta(seconds=self.tau_max)
        end = window.end_et - timedelta(seconds=self.tau_min)
        if end < start:
            return ReplayPlan(eval_times=[])
        if self.replay_cadence_seconds <= 0:
            raise ValueError("replay_cadence_seconds must be > 0")
        eval_times = []
        current = start
        step = timedelta(seconds=self.replay_cadence_seconds)
        while current <= end:
            eval_times.append(current)
            current += step
        return ReplayPlan(eval_times=eval_times, stop_on_signal=True, cadence_seconds=self.replay_cadence_seconds)

    def on_window_close(self, window: Any, context: Context) -> Optional[Signal]:
        return None
