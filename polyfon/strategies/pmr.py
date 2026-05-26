"""Strategy: Price Momentum Reversal (PMR)."""
from datetime import timedelta
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class PMRStrategy(BaseStrategy):
    """Price Momentum Reversal.

    Detects when an intra-window price extreme has been reached and the
    spot has already pulled back by a meaningful fraction of the range.
    This signals that the initial momentum has exhausted and a reversal
    toward the mean is underway.

    PMR is the complement of ROM:
      - ROM enters when price is AT an extreme expecting continuation.
      - PMR enters when price has PULLED BACK from an extreme, betting on
        full reversion toward the opposite bound.

    The pullback is measured as a fraction of the established intra-window
    range width.  A pullback of 30%+ from the extreme suggests the momentum
    phase is over and a reversal leg is in progress.

    Parameters (empirically discoverable):
        theta_reversal:  Min pullback from extreme (fraction of range width)
                         to consider a reversal confirmed.  Default 0.30.
        theta_sat:       Pullback fraction at which confidence saturates.
                         Default 0.50.
        tau_max:         Latest entry before resolution (default 90s).
        tau_min:         Earliest entry before resolution (default 15s).
        q_max:           Position size.  For *market* class = USDC to spend;
                         for *limit* class = number of shares.
        theta_edge:      Minimum expected edge per share to trade.
        order_class:     'market' (default) or 'limit'.
        time_in_force:   'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
                         Market -> FOK or FAK.  Limit -> GTC or GTD.
        fee_rate:        Market taker fee rate.
    """

    name = "PMR"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        theta_reversal: float = 0.30,
        theta_sat: float = 0.50,
        tau_max: float = 90.0,
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
        self.theta_reversal = theta_reversal
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

        # Pullback from extremes as a fraction of range width.
        #   pullback_from_high = 0  → still at the high
        #   pullback_from_high = 1  → all the way back to the low
        #   bounce_from_low    = 0  → still at the low
        #   bounce_from_low    = 1  → all the way back to the high
        pullback_from_high = (r_high - spot) / r_width
        bounce_from_low = (spot - r_low) / r_width

        # Price has pulled back from the range high → reversal down → BUY_NO.
        if pullback_from_high > self.theta_reversal and context.down_best_ask is not None:
            price = context.down_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(pullback_from_high / self.theta_sat, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "range_high": r_high, "range_low": r_low,
                        "pullback_from_high": pullback_from_high,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max, "notional": notional,
                    },
                )

        # Price has bounced from the range low → reversal up → BUY_YES.
        if bounce_from_low > self.theta_reversal and context.up_best_ask is not None:
            price = context.up_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(bounce_from_low / self.theta_sat, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "range_high": r_high, "range_low": r_low,
                        "bounce_from_low": bounce_from_low,
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
