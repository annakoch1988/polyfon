"""Strategy: Range Oscillation Momentum (ROM)."""
from datetime import timedelta
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class ROMStrategy(BaseStrategy):
    number = 11
    """Range Oscillation Momentum.

    Exploits intra-window spot price ranges.  When price has established
    a well-defined range and is positioned near one extreme at evaluation
    time, a breakout/momentum continuation is likely.  The strategy enters
    in the direction of the extreme.

    The core signal is the *proximity* of current spot to the range bounds.
    Spot near range-high → momentum up → BUY YES.  Spot near range-low →
    momentum down → BUY NO.  Range width vs spot volatility provides the
    confidence weight (tight ranges give stronger signals).

    Parameters (empirically discoverable):
        theta_entry:  Minimum |delta| relative to range to enter (default 0.001).
        theta_sat:    |delta| magnitude at which confidence saturates (default 0.003).
        tau_max:      Latest entry before resolution (default 120s).
        tau_min:      Earliest entry before resolution (default 30s).
        q_max:        Position size.  For *market* class = USDC to spend;
                      for *limit* class = number of shares.
        theta_edge:   Minimum expected edge per share to trade.
        order_class:  'market' (default) or 'limit'.  ROM enters mid-window
                      so limit orders are feasible but market is also valid.
        time_in_force: 'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
                       Market → FOK or FAK.  Limit → GTC or GTD.
        fee_rate:     Market taker fee rate.
    """

    name = "ROM"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        theta_entry: float = 0.001,
        theta_sat: float = 0.003,
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
        if context.spot_price is None:
            return None
        if context.range_high is None or context.range_low is None:
            return None
        if context.tau_seconds is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        spot = context.spot_price
        r_high = context.range_high
        r_low = context.range_low
        r_width = r_high - r_low
        if r_width <= 0:
            return None

        # Proximity to range bounds (0 = at the bound, 1 = at the opposite).
        prox_up = (r_high - spot) / r_width
        prox_down = (spot - r_low) / r_width

        # Range-normalised delta — fraction-of-range displacement from the opposite extreme.
        # When spot is near the high, delta_up is positive and large.
        # When spot is near the low, delta_down is positive and large.
        delta_up = (spot - r_low) / r_low if r_low > 0 else 0.0
        delta_down = (r_high - spot) / r_high if r_high > 0 else 0.0

        # Entry: spot is near the range high (momentum up) → BUY_YES.
        if prox_up < 0.2 and delta_up > self.theta_entry and context.up_best_ask is not None:
            price = context.up_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(delta_up / self.theta_sat, 1.0) * min(r_width / (0.5 * r_low) if r_low > 0 else 1.0, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "range_high": r_high, "range_low": r_low,
                        "delta_up": delta_up, "prox_up": prox_up,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max, "notional": notional,
                    },
                )

        # Entry: spot is near the range low (momentum down) → BUY_NO.
        if prox_down < 0.2 and delta_down > self.theta_entry and context.down_best_ask is not None:
            price = context.down_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(delta_down / self.theta_sat, 1.0) * min(r_width / (0.5 * r_low) if r_low > 0 else 1.0, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "range_high": r_high, "range_low": r_low,
                        "delta_down": delta_down, "prox_down": prox_down,
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
