"""Strategy: Window Delta Momentum (WDM)."""
from datetime import timedelta
from typing import Any, Optional

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class WDMStrategy(BaseStrategy):
    number = 13
    """Window Delta Momentum.

    Entry at T-10s based on spot displacement from window open price.
    If delta exceeds threshold, outcome is near-deterministic at that horizon.

    Parameters (empirically discoverable):
        theta_entry:  Minimum |delta| to enter (default 0.001 = 0.10%).
        theta_sat:    Delta magnitude at which confidence saturates (default 0.003).
        tau_max:      Latest entry before resolution (default 15s).
        tau_min:      Earliest entry before resolution (default 5s).
        q_max:        Position size.  For *market* class = USDC to spend;
                      for *limit* class = number of shares.
        theta_edge:   Minimum expected edge per share to trade.
        order_class:  'market' (default) or 'limit'.  WDM at T-10s needs
                      immediate execution → market.
        time_in_force: 'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
                       Market → FOK or FAK.  Limit → GTC or GTD.
        fee_rate:     Market taker fee rate.
    """

    name = "WDM"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        theta_entry: float = 0.001,
        theta_sat: float = 0.003,
        tau_max: float = 15.0,
        tau_min: float = 5.0,
        replay_cadence_seconds: float = 1.0,
        q_max: float = 100.0,
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
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=confidence,
                    metadata={
                        "delta": delta,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max,
                        "notional": notional,
                        "window_open": context.window_open_price,
                    },
                )

        if delta < -self.theta_entry and context.down_best_ask is not None:
            price = context.down_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=confidence,
                    metadata={
                        "delta": delta,
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max,
                        "notional": notional,
                        "window_open": context.window_open_price,
                    },
                )

        return None

    def build_replay_plan(self, window: Any) -> ReplayPlan:
        return ReplayPlan(
            eval_times=[window.end_et - timedelta(seconds=10)],
            cadence_seconds=self.replay_cadence_seconds,
        )

    def on_window_close(self, window: Any, context: Context) -> Optional[Signal]:
        return None
