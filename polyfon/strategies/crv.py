"""Strategy: Cross-Contract Relative Value (CRV)."""
from datetime import timedelta
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class CRVStrategy(BaseStrategy):
    """Cross-Contract Relative Value.

    The original CRV proposal requires multiple parallel binary contracts
    with different strikes.  In the single-strike up/down model used
    here we adapt the idea to a **consistency check**: the YES price
    must respect the monotonic relationship with spot price.

    For a binary call on "spot > open" the fair probability is
    monotonically increasing in spot.  A basic consistency check
    is therefore:

      * spot > strike  →  YES price should be > reference_level
      * spot < strike  →  YES price should be < reference_level

    When the market price violates this expectation by more than
    theta_entry the strategy enters in the direction of the fair
    value:  BUY_YES when the price sits too cheaply above the
    strike, BUY_NO when it sits too richly below it.

    Parameters (empirically discoverable):
        theta_entry:    Minimum |mispricing| from the consistency
                        boundary to enter (default 0.05).
        reference_level: Probability level that separates the
                        "above strike" region from "below strike".
                        Default 0.50 (the natural at-the-money
                        boundary for a binary call).
        tau_max:        Latest entry before resolution (default 240s).
        tau_min:        Earliest entry before resolution (default 15s).
        q_max:          Position size.  For *market* class = USDC to spend;
                        for *limit* class = number of shares.
        theta_edge:     Minimum expected edge per share to trade.
        order_class:    'market' (default) or 'limit'.
        time_in_force:  'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
                        Market → FOK or FAK.  Limit → GTC or GTD.
        fee_rate:       Market taker fee rate.
    """

    name = "CRV"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        theta_entry: float = 0.05,
        reference_level: float = 0.50,
        tau_max: float = 240.0,
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
        if not 0.0 < reference_level < 1.0:
            raise ValueError(f"reference_level must be in (0,1), got {reference_level}")
        self.theta_entry = theta_entry
        self.reference_level = reference_level
        self.tau_max = tau_max
        self.tau_min = tau_min
        self.replay_cadence_seconds = replay_cadence_seconds
        self.q_max = q_max
        self.theta_edge = theta_edge
        self.order_class = order_class
        self.time_in_force = time_in_force
        self.fee_rate = fee_rate

    def _resolve_order(self, price: float) -> tuple[float, float] | None:
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
        if context.spot_price is None or context.window_open_price is None:
            return None
        if context.tau_seconds is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        spot = context.spot_price
        strike = context.window_open_price

        # Use YES ask as the market-implied probability.
        up_ask = context.up_best_ask
        if up_ask is None:
            return None

        # Monotonicity-consistency: does the side of the strike match
        # the side of the market price?
        if spot > strike:
            # Spot is above strike → YES should be priced above reference_level.
            mispricing = self.reference_level - up_ask
            if mispricing < self.theta_entry:
                return None
            resolved = self._resolve_order(up_ask)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, up_ask, self.fee_rate)
            edge = mispricing - fee / shares
            if edge > self.theta_edge:
                confidence = min(mispricing / self.theta_entry, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "spot": spot,
                        "strike": strike,
                        "reference_level": self.reference_level,
                        "up_ask": up_ask,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max,
                        "notional": notional,
                    },
                )

        elif spot < strike:
            # Spot is below strike → YES should be priced below reference_level.
            mispricing = up_ask - self.reference_level
            if mispricing < self.theta_entry:
                return None
            resolved = self._resolve_order(up_ask)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, up_ask, self.fee_rate)
            edge = mispricing - fee / shares
            if edge > self.theta_edge:
                # Mispricing means YES is too expensive; go the other way.
                # But we still need down_best_ask for execution.
                if context.down_best_ask is None:
                    return None
                shares, notional = self._resolve_order(context.down_best_ask)
                if shares is None:
                    return None
                fee = taker_fee_usdc(shares, context.down_best_ask, self.fee_rate)
                edge = mispricing - fee / shares
                if edge > self.theta_edge:
                    confidence = min(mispricing / self.theta_entry, 1.0)
                    return Signal(
                        strategy=self.name,
                        direction="BUY_NO",
                        size=shares,
                        expected_edge=float(edge),
                        confidence=float(min(confidence, 1.0)),
                        metadata={
                            "spot": spot,
                            "strike": strike,
                            "reference_level": self.reference_level,
                            "up_ask": up_ask,
                            "tau": tau,
                            "order_class": self.order_class,
                            "time_in_force": self.time_in_force,
                            "q_max": self.q_max,
                            "notional": notional,
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
