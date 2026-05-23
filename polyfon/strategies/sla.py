"""Strategy 1: Spot-Led Latency Arbitrage (SLA)."""
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class SLAStrategy(BaseStrategy):
    """Spot-Led Latency Arbitrage.

    Parameters (empirically discoverable):
        theta_entry: Minimum mispricing to enter (default 0.05).
        tau_min: Don't trade if less than tau_min seconds to resolution.
        q_max: Maximum position size in shares.
        fee_rate: Market taker fee rate.
    """

    name = "SLA"

    def __init__(
        self,
        theta_entry: float = 0.05,
        tau_min: float = 30.0,
        q_max: float = 100.0,
        fee_rate: float = 0.07,
    ):
        self.theta_entry = theta_entry
        self.tau_min = tau_min
        self.q_max = q_max
        self.fee_rate = fee_rate

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
            fee = taker_fee_usdc(self.q_max, price, self.fee_rate)
            edge = mispricing - fee / self.q_max
            if edge > 0:
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=self.q_max,
                    expected_edge=float(edge),
                    confidence=min(mispricing / 0.20, 1.0),
                    metadata={"fair_prob": fair_prob, "market_price": price, "tau": context.tau_seconds},
                )

        if mispricing < -self.theta_entry and context.best_bid is not None:
            price = context.best_bid
            fee = taker_fee_usdc(self.q_max, price, self.fee_rate)
            edge = -mispricing - fee / self.q_max
            if edge > 0:
                return Signal(
                    strategy=self.name,
                    direction="SELL_YES",
                    size=self.q_max,
                    expected_edge=float(edge),
                    confidence=min(-mispricing / 0.20, 1.0),
                    metadata={"fair_prob": fair_prob, "market_price": price, "tau": context.tau_seconds},
                )

        return None

    def on_window_close(self, window: Any, context: Context) -> Optional[Signal]:
        return None
