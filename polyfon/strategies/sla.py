"""Strategy 1: Spot-Led Latency Arbitrage (SLA)."""
from datetime import timedelta
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class SLAStrategy(BaseStrategy):
    """Spot-Led Latency Arbitrage.

    Parameters (empirically discoverable):
        theta_entry:  Minimum mispricing to enter (default 0.05).
        tau_min:      Don't trade if less than tau_min seconds to resolution.
        q_max:        Position size.  For *market* class = USDC to spend;
                      for *limit* class = number of shares.
        order_class:  'limit' (default) or 'market'.  SLA has 30s+ runway
                      so limit orders (rest on book) are the natural fit.
        time_in_force: 'GTC' (default for limit), 'GTD', 'FOK', 'FAK'.
                       Limit → GTC or GTD.  Market → FOK or FAK.
        fee_rate:     Market taker fee rate.
    """

    name = "SLA"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        theta_entry: float = 0.05,
        tau_min: float = 30.0,
        replay_cadence_seconds: float = 1.0,
        q_max: float = 100.0,
        order_class: str = "limit",
        time_in_force: str = "GTC",
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
        self.tau_min = tau_min
        self.replay_cadence_seconds = replay_cadence_seconds
        self.q_max = q_max
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
        if context.spot_price is None or context.fair_probability is None:
            return None
        if context.best_ask is None and context.best_bid is None:
            return None
        if context.tau_seconds is None or context.tau_seconds < self.tau_min:
            return None

        fair_prob = context.fair_probability
        market_price = context.best_ask if context.best_ask else context.best_bid
        if market_price is None:
            return None

        mispricing = fair_prob - market_price
        if mispricing > self.theta_entry and context.best_ask is not None:
            price = context.best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = mispricing - fee / shares
            if edge > 0:
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=min(mispricing / 0.20, 1.0),
                    metadata={
                        "fair_prob": fair_prob, "market_price": price,
                        "tau": context.tau_seconds, "notional": notional,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max,
                    },
                )

        if mispricing < -self.theta_entry and context.best_bid is not None:
            price = context.best_bid
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = -mispricing - fee / shares
            if edge > 0:
                return Signal(
                    strategy=self.name,
                    direction="SELL_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=min(-mispricing / 0.20, 1.0),
                    metadata={
                        "fair_prob": fair_prob, "market_price": price,
                        "tau": context.tau_seconds, "notional": notional,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max,
                    },
                )

        return None

    def build_replay_plan(self, window: Any) -> ReplayPlan:
        latest = window.end_et - timedelta(seconds=self.tau_min)
        if latest <= window.start_et:
            return ReplayPlan(eval_times=[])
        if self.replay_cadence_seconds <= 0:
            raise ValueError("replay_cadence_seconds must be > 0")
        eval_times = []
        current = window.start_et
        step = timedelta(seconds=self.replay_cadence_seconds)
        while current <= latest:
            eval_times.append(current)
            current += step
        return ReplayPlan(eval_times=eval_times, stop_on_signal=True, cadence_seconds=self.replay_cadence_seconds)

    def on_window_close(self, window: Any, context: Context) -> Optional[Signal]:
        return None
