"""Strategy: CEX Toxicity as a Leading Volatility Indicator (VPX)."""
from datetime import timedelta
from typing import Any, Optional

import numpy as np
from scipy import stats as sp_stats

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class VPXStrategy(BaseStrategy):
    number = 8
    """CEX Toxicity as a Leading Volatility Indicator (VPX, a.k.a. VPIN-X).

    Detects volatility regime shifts by comparing short-term realised
    volatility to a longer-term baseline.  When short-term vol exceeds
    the baseline by a configurable threshold a volatility regime shift
    is predicted.  Contracts are repriced using the projected vol and
    traded against the market price.

    Adaptation from the original VPIN-X which required Binance trade-
    level data for Volume-synchronized Probability of Informed Trading.
    Here, the ratio of short-window to long-window realised volatility
    stands in for the VPIN spike detection.

    Parameters (empirically discoverable):
        vpx_threshold:  Min ratio sigma_short / sigma_long to detect a
                        volatility spike (default 1.5).
        beta_vpx:       Persistence of the vol spike — how much of the
                        short-term elevation carries forward
                        (default 0.5).
        theta_entry:    Min |mispricing| between VPX-adjusted fair prob
                        and market price (default 0.03).
        tau_max:        Latest entry before resolution (default 240s).
        tau_min:        Earliest entry before resolution (default 15s).
        q_max:          Position size (USDC for market orders).
        theta_edge:     Minimum expected edge per share.
        order_class:    'market' (default) or 'limit'.
        time_in_force:  'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
        fee_rate:       Market taker fee rate.
    """

    name = "VPX"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        vpx_threshold: float = 1.5,
        beta_vpx: float = 0.5,
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
        if vpx_threshold <= 1.0:
            raise ValueError(f"vpx_threshold must be > 1.0, got {vpx_threshold}")
        self.vpx_threshold = vpx_threshold
        self.beta_vpx = beta_vpx
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
        if context.sigma_per_minute is None or context.sigma_short_per_minute is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        sigma_long = context.sigma_per_minute
        sigma_short = context.sigma_short_per_minute

        if sigma_long <= 0 or sigma_short <= 0:
            return None

        vol_ratio = sigma_short / sigma_long

        if vol_ratio < self.vpx_threshold:
            return None

        # Project volatility forward — the spike partially persists.
        projected_sigma = sigma_long + self.beta_vpx * (sigma_short - sigma_long)

        # VPX-adjusted fair probability using projected vol.
        tau_minutes = tau / 60.0
        sigma_total = projected_sigma * np.sqrt(tau_minutes)
        if sigma_total <= 0:
            return None

        d1 = np.log(context.spot_price / context.window_open_price) / sigma_total
        adjusted_prob = float(sp_stats.norm.cdf(d1))
        adjusted_prob = np.clip(adjusted_prob, 0.001, 0.999)

        # Mispricing vs market.
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
                confidence = min((vol_ratio - 1.0) / (self.vpx_threshold - 1.0), 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_YES",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "vol_ratio": float(vol_ratio),
                        "projected_sigma": float(projected_sigma),
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
                confidence = min((vol_ratio - 1.0) / (self.vpx_threshold - 1.0), 1.0)
                return Signal(
                    strategy=self.name,
                    direction="BUY_NO",
                    size=shares,
                    expected_edge=float(edge),
                    confidence=float(min(confidence, 1.0)),
                    metadata={
                        "vol_ratio": float(vol_ratio),
                        "projected_sigma": float(projected_sigma),
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
