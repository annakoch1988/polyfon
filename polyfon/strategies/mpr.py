"""Strategy: Mean Price Reversion (MPR)."""
from datetime import timedelta
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class MPRStrategy(BaseStrategy):
    """Mean Price Reversion.

    Enters when the current spot price has deviated significantly from
    the intra-window mean spot price.  The core assumption is that
    short-lived momentum spikes revert toward the mean within the 5-minute
    window, and the Polymarket token price lags the spot move.

    MPR is distinct from ROM and PMR:
      - ROM enters at the extreme expecting continuation.
      - PMR enters after a pullback from the extreme.
      - MPR enters on deviation from the mean without requiring an
        extreme to have been reached first.

    The deviation is measured as a fraction of the mean:
        deviation = (spot - mean_price) / mean_price

    Positive deviation → spot above mean → expect reversion down → BUY_NO.
    Negative deviation → spot below mean → expect reversion up → BUY_YES.

    Parameters (empirically discoverable):
        theta_entry:  Min |deviation| from mean to consider entry (default 0.0002).
        theta_sat:    |deviation| at which confidence saturates (default 0.0006).
        tau_max:      Latest entry before resolution (default 120s).
        tau_min:      Earliest entry before resolution (default 30s).
        q_max:        Position size.  For *market* class = USDC to spend;
                      for *limit* class = number of shares.
        theta_edge:   Minimum expected edge per share to trade.
        order_class:  'market' (default) or 'limit'.
        time_in_force: 'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
                       Market -> FOK or FAK.  Limit -> GTC or GTD.
        fee_rate:     Market taker fee rate.
    """

    name = "MPR"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        theta_entry: float = 0.0002,
        theta_sat: float = 0.0006,
        tau_max: float = 120.0,
        tau_min: float = 30.0,
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
        if context.spot_price is None or context.mean_spot_price is None:
            return None
        if context.tau_seconds is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        mean_price = context.mean_spot_price
        if mean_price <= 0:
            return None

        deviation = (context.spot_price - mean_price) / mean_price
        if abs(deviation) < self.theta_entry:
            return None

        if deviation > self.theta_entry and context.down_best_ask is not None:
            price = context.down_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(deviation / self.theta_sat, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "deviation": deviation,
                        "mean_price": mean_price,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max, "notional": notional,
                    },
                )

        if deviation < -self.theta_entry and context.up_best_ask is not None:
            price = context.up_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(-deviation / self.theta_sat, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "deviation": deviation,
                        "mean_price": mean_price,
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
