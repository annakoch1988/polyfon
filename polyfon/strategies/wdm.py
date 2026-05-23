"""Strategy: Window Delta Momentum (WDM)."""
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class WDMStrategy(BaseStrategy):
    """Window Delta Momentum.

    Entry at T-10s based on spot displacement from window open price.
    If delta exceeds threshold, outcome is near-deterministic at that horizon.

    Parameters (empirically discoverable):
        theta_entry: Minimum |delta| to enter (default 0.001 = 0.10%).
        theta_sat:   Delta magnitude at which confidence saturates (default 0.003).
        tau_max:     Latest entry before resolution (default 15s).
        tau_min:     Earliest entry before resolution (default 5s).
        q_max:       Maximum position size in shares.
        theta_edge:  Minimum expected edge per share to trade.
        fee_rate:    Market taker fee rate.
    """

    name = "WDM"

    def __init__(
        self,
        theta_entry: float = 0.001,
        theta_sat: float = 0.003,
        tau_max: float = 15.0,
        tau_min: float = 5.0,
        q_max: float = 100.0,
        theta_edge: float = 0.01,
        fee_rate: float = 0.07,
    ):
        self.theta_entry = theta_entry
        self.theta_sat = theta_sat
        self.tau_max = tau_max
        self.tau_min = tau_min
        self.q_max = q_max
        self.theta_edge = theta_edge
        self.fee_rate = fee_rate

    def on_tick(self, window: Any, context: Context) -> Optional[Signal]:
        if context.spot_price is None or context.window_open_price is None:
            return None
        if context.tau_seconds is None:
            return None

        tau = context.tau_seconds
        if tau > self.tau_max or tau < self.tau_min:
            return None

        delta = (context.spot_price - context.window_open_price) / context.window_open_price
        if abs(delta) < self.theta_entry:
            return None

        confidence = min(abs(delta) / self.theta_sat, 1.0)

        if delta > self.theta_entry and context.up_best_ask is not None:
            price = context.up_best_ask
            fee = taker_fee_usdc(self.q_max, price, self.fee_rate)
            edge = (1.0 - price) - fee / self.q_max
            if edge > self.theta_edge:
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=self.q_max,
                    expected_edge=float(edge),
                    confidence=confidence,
                    metadata={
                        "delta": delta,
                        "tau": tau,
                        "window_open": context.window_open_price,
                    },
                )

        if delta < -self.theta_entry and context.down_best_ask is not None:
            price = context.down_best_ask
            fee = taker_fee_usdc(self.q_max, price, self.fee_rate)
            edge = (1.0 - price) - fee / self.q_max
            if edge > self.theta_edge:
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=self.q_max,
                    expected_edge=float(edge),
                    confidence=confidence,
                    metadata={
                        "delta": delta,
                        "tau": tau,
                        "window_open": context.window_open_price,
                    },
                )

        return None

    def on_window_close(self, window: Any, context: Context) -> Optional[Signal]:
        return None
