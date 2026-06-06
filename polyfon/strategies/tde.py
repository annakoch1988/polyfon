"""Strategy: Time Decay Effect (TDE)."""
import math
from datetime import timedelta
from typing import Any, Optional

from scipy.stats import norm

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


def _theta(
    spot: float,
    strike: float,
    tau_seconds: float,
    sigma_per_minute: float,
) -> float:
    """Rate of probability change per second (∂π̂/∂τ).

    Uses the Black-Scholes binary call theta formula with drift=0:
        Θ = φ(d₁) · ∂d₁/∂τ

    Parameters are the same as fair_probability() so that theta is
    consistent with the Context's fair_probability value.
    """
    if tau_seconds <= 1.0 or sigma_per_minute == 0:
        return 0.0
    tau_min = tau_seconds / 60.0
    sigma_tau = sigma_per_minute * math.sqrt(tau_min)
    if sigma_tau == 0:
        return 0.0

    log_moneyness = math.log(spot / strike)
    d1 = (log_moneyness - 0.5 * sigma_per_minute ** 2 * tau_min) / sigma_tau
    phi_d1 = float(norm.pdf(d1))

    # ∂d₁/∂τ_min (per-minute base)
    tau_pow = tau_min ** 1.5
    d_d1_d_tau_min = -0.5 * log_moneyness / (sigma_per_minute * tau_pow) - 0.25 * sigma_per_minute / math.sqrt(tau_min)

    # Convert to per-second
    d_d1_d_tau = d_d1_d_tau_min / 60.0
    return phi_d1 * d_d1_d_tau


@register
class TDEStrategy(BaseStrategy):
    number = 5
    """Time Decay Effect.

    Exploits accelerating probability convergence as τ → 0.
    When the fair probability drifts deterministically toward 0 or 1
    in the final seconds, and the Polymarket price has not kept up,
    the strategy enters in the direction of the drift.

    The "double condition" requires:
      1. |fair_prob - market_price| > theta_entry  (level mispricing)
      2. sign(Θ) == sign(fair_prob - market_price)  (mispricing is widening)

    Parameters (empirically discoverable):
        theta_entry:  Minimum |mispricing| to enter (default 0.05).
        tau_max:      Latest entry before resolution (default 90s).
        tau_min:      Earliest entry before resolution (default 15s).
        epsilon_sat:  Mispricing level at which confidence saturates (default 0.15).
        theta_sat:    Theta magnitude at which rate-confidence saturates (default 0.005).
        q_max:        Position size.  For *market* class = USDC to spend;
                      for *limit* class = number of shares.
        theta_edge:   Minimum expected edge per share to trade.
        order_class:  'market' (default) or 'limit'.  TDE near resolution
                      needs immediate execution → market.
        time_in_force: 'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
                       Market → FOK or FAK.  Limit → GTC or GTD.
        fee_rate:     Market taker fee rate.
    """

    name = "TDE"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        theta_entry: float = 0.05,
        tau_max: float = 90.0,
        tau_min: float = 15.0,
        replay_cadence_seconds: float = 1.0,
        epsilon_sat: float = 0.15,
        theta_sat: float = 0.005,
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
        self.tau_max = tau_max
        self.tau_min = tau_min
        self.replay_cadence_seconds = replay_cadence_seconds
        self.epsilon_sat = epsilon_sat
        self.theta_sat = theta_sat
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
        if context.spot_price is None or context.fair_probability is None:
            return None
        if context.tau_seconds is None:
            return None
        if context.window_open_price is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        fair_prob = context.fair_probability
        strike = context.window_open_price

        # Compute theta using engine's sigma_per_minute
        sigma = context.sigma_per_minute or 0.001
        theta = _theta(context.spot_price, strike, tau, sigma)

        # Determine market price and direction
        # If theta < 0 → probability rising → BUY_YES
        # If theta > 0 → probability falling → BUY_NO
        if theta < -1e-12 and context.up_best_ask is not None:
            price = context.up_best_ask
            mispricing = fair_prob - price
            if mispricing < self.theta_entry:
                return None
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = mispricing - fee / shares
            if edge < self.theta_edge:
                return None
            confidence = min(mispricing / self.epsilon_sat, 1.0) * min(abs(theta) / self.theta_sat, 1.0)
            return Signal(
                strategy=self.name,
                direction="BUY_YES",
                size=shares,
                expected_edge=float(edge),
                confidence=float(min(confidence, 1.0)),
                metadata={
                    "fair_prob": fair_prob, "market_price": price,
                    "theta": theta, "tau": tau,
                    "order_class": self.order_class,
                    "time_in_force": self.time_in_force,
                    "q_max": self.q_max, "notional": notional,
                },
            )

        if theta > 1e-12 and context.down_best_ask is not None:
            price = context.down_best_ask
            mispricing = (1.0 - fair_prob) - price
            if mispricing < self.theta_entry:
                return None
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = mispricing - fee / shares
            if edge < self.theta_edge:
                return None
            confidence = min(mispricing / self.epsilon_sat, 1.0) * min(abs(theta) / self.theta_sat, 1.0)
            return Signal(
                strategy=self.name,
                direction="BUY_NO",
                size=shares,
                expected_edge=float(edge),
                confidence=float(min(confidence, 1.0)),
                metadata={
                    "fair_prob": fair_prob, "market_price": price,
                    "theta": theta, "tau": tau,
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
