"""Strategy: Cross-Asset Correlation Lead-Lag (CLL)."""
from datetime import timedelta
from typing import Any, Optional

import numpy as np
from scipy import stats as sp_stats

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class CLLStrategy(BaseStrategy):
    """Cross-Asset Correlation Lead-Lag.

    Exploits persistent asymmetric lead-lag dynamics between crypto assets
    (e.g. BTC → ETH).  When the leader asset moves significantly, the
    lagger's Polymarket contract has not yet repriced — the strategy enters
    in the direction of the predicted catch-up move.

    The adjusted fair probability is computed using the leader's recent
    return to predict the lagger's future spot, with conditional volatility
    to account for the explained variance.

    Parameters (empirically discoverable):
        leader:         Symbol of the leader asset (e.g. "BTC").
        lookback_seconds: Window over which to compute leader return (default 60s).
        beta_lead:      Regression coefficient — lagger return per unit leader
                        return (default 0.35).
        rho:            Cross-correlation between leader and lagger returns
                        (default 0.72).  Used for conditional volatility.
        leader_min_return: Minimum |leader return| to act (default 0.001).
        theta_entry:    Min |mispricing| between CLL-adjusted fair prob and
                        market price (default 0.03).
        tau_max:        Latest entry before resolution (default 240s).
        tau_min:        Earliest entry before resolution (default 15s).
        q_max:          Position size (USDC for market orders).
        theta_edge:     Minimum expected edge per share.
        order_class:    'market' (default) or 'limit'.
        time_in_force:  'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
        fee_rate:       Market taker fee rate.
    """

    name = "CLL"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        leader: str = "BTC",
        lookback_seconds: float = 60.0,
        beta_lead: float = 0.35,
        rho: float = 0.72,
        leader_min_return: float = 0.001,
        theta_entry: float = 0.03,
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
        if not (0 < rho < 1):
            raise ValueError(f"rho must be in (0,1), got {rho}")
        self.leader = leader.upper()
        self.lookback_seconds = lookback_seconds
        self.beta_lead = beta_lead
        self.rho = rho
        self.leader_min_return = leader_min_return
        self.theta_entry = theta_entry
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
        if context.leader_return is None or context.leader_spot_price is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        # Skip if the current window IS the leader (no lead-lag for the leader itself).
        if window.underlying.upper() == self.leader:
            return None

        if abs(context.leader_return) < self.leader_min_return:
            return None

        # Predict lagger return from leader return.
        predicted_return = self.beta_lead * context.leader_return

        # Predicted spot price at resolution.
        predicted_spot = context.spot_price * np.exp(predicted_return)

        # Conditional volatility: sigma * sqrt(1 - rho^2).
        sigma = context.sigma_per_minute or 0.001
        cond_sigma = sigma * np.sqrt(1.0 - self.rho * self.rho)

        # CLL-adjusted fair probability.
        d = np.log(predicted_spot / context.window_open_price)
        if cond_sigma * np.sqrt(tau / 60.0) > 0:
            d /= cond_sigma * np.sqrt(tau / 60.0)
        adjusted_prob = float(sp_stats.norm.cdf(d))
        adjusted_prob = np.clip(adjusted_prob, 0.001, 0.999)

        # Mispricing.
        up_ask = context.up_best_ask
        if up_ask is None:
            return None
        market_price = up_ask
        mispricing = adjusted_prob - market_price

        if abs(mispricing) < self.theta_entry:
            return None

        if mispricing > 0:
            price = up_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(abs(context.leader_return) / self.leader_min_return, 1.0) * (
                    1.0 - self.rho
                )
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "leader_return": context.leader_return,
                        "predicted_return": predicted_return,
                        "predicted_spot": float(predicted_spot),
                        "adjusted_prob": float(adjusted_prob),
                        "mispricing": float(mispricing),
                        "tau": tau,
                        "order_class": self.order_class,
                        "time_in_force": self.time_in_force,
                        "q_max": self.q_max,
                        "notional": notional,
                    },
                )

        else:
            if context.down_best_ask is None:
                return None
            price = context.down_best_ask
            resolved = self._resolve_order(price)
            if resolved is None:
                return None
            shares, notional = resolved
            fee = taker_fee_usdc(shares, price, self.fee_rate)
            edge = (1.0 - price) - fee / shares
            if edge > self.theta_edge:
                confidence = min(abs(context.leader_return) / self.leader_min_return, 1.0) * (
                    1.0 - self.rho
                )
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "leader_return": context.leader_return,
                        "predicted_return": predicted_return,
                        "predicted_spot": float(predicted_spot),
                        "adjusted_prob": float(adjusted_prob),
                        "mispricing": float(mispricing),
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
