"""Strategy: Perpetual Funding Rate Sentiment Arbitrage (PFR)."""
import math
from datetime import timedelta
from typing import Any, Optional

from scipy.stats import norm

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class PFRStrategy(BaseStrategy):
    number = 14
    """Perpetual Funding Rate Sentiment Arbitrage.

    Crypto perpetual funding rate extremes predict short-term spot
    reversals beyond what the baseline Black-Scholes fair probability
    can capture.  Polymarket retail flow adjusts to funding information
    with a lag, creating a tradable mispricing window.

    The strategy adjusts the fair probability using the funding-rate
    sentiment index:

        Z   = (f_t - mu_f) / sigma_f
        Psi = -tanh(Z / lambda_f)
        S_hat = S * exp(beta_f * Psi * sigma_spot * sqrt(tau))

    Entry conditions:
        |Z|          >  z_min        (funding is extreme)
        |pi_hat - p| >  theta_entry  (mispricing)

    Parameters:
        mu_f:          Rolling mean of funding rate (default 0.0).
        sigma_f:       Rolling std of funding rate (default 0.0025).
        lambda_f:      Sentiment scaling (default 2.0).
        beta_f:        Spot impact coefficient (default 0.8).
        z_min:         Minimum |Z-score| to consider entry (default 2.0).
        theta_entry:   Minimum |mispricing| (default 0.05).
        tau_max:       Latest entry before resolution (default 120s).
        tau_min:       Earliest entry before resolution (default 15s).
        funding_rate:  Current predicted funding rate (default 0.0).
        q_max:         Position size in USDC (market orders).
        theta_edge:    Minimum expected edge per share.
        order_class:   'market' (default) or 'limit'.
        time_in_force: 'FOK' (default), 'FAK', 'GTC', 'GTD'.
        fee_rate:      Market taker fee rate.
    """

    name = "PFR"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        mu_f: float = 0.0,
        sigma_f: float = 0.0025,
        lambda_f: float = 2.0,
        beta_f: float = 0.8,
        z_min: float = 2.0,
        theta_entry: float = 0.05,
        tau_max: float = 120.0,
        tau_min: float = 15.0,
        replay_cadence_seconds: float = 1.0,
        funding_rate: float = 0.0,
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
        self.mu_f = mu_f
        self.sigma_f = sigma_f
        self.lambda_f = lambda_f
        self.beta_f = beta_f
        self.z_min = z_min
        self.theta_entry = theta_entry
        self.tau_max = tau_max
        self.tau_min = tau_min
        self.replay_cadence_seconds = replay_cadence_seconds
        self.funding_rate = funding_rate
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

        if self.sigma_f <= 0:
            return None

        z_f = (self.funding_rate - self.mu_f) / self.sigma_f
        if abs(z_f) < self.z_min:
            return None

        psi = -math.tanh(z_f / self.lambda_f)

        sigma = context.sigma_per_minute or 0.001
        tau_minutes = tau / 60.0
        sigma_tau = sigma * math.sqrt(tau_minutes)
        strike = context.window_open_price

        predicted_spot = context.spot_price * math.exp(
            self.beta_f * psi * sigma * math.sqrt(tau_minutes)
        )

        d = math.log(predicted_spot / strike) / sigma_tau if sigma_tau > 0 else 0.0
        fair_prob = float(norm.cdf(d))
        fair_prob = max(0.001, min(0.999, fair_prob))

        up_ask = context.up_best_ask
        if up_ask is None:
            return None
        market_price = up_ask
        mispricing = fair_prob - market_price

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
                confidence = min(abs(mispricing) / 0.15, 1.0) * min(abs(z_f) / self.z_min, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "z_f": z_f,
                        "psi": psi,
                        "fair_prob": fair_prob,
                        "mispricing": mispricing,
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
                confidence = min(abs(mispricing) / 0.15, 1.0) * min(abs(z_f) / self.z_min, 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "z_f": z_f,
                        "psi": psi,
                        "fair_prob": fair_prob,
                        "mispricing": mispricing,
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
