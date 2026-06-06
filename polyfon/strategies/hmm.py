"""Strategy: Hidden Markov Model Regime-Switching (HMM-RS)."""
from datetime import timedelta
from typing import Any, Optional

import numpy as np

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class HMMStrategy(BaseStrategy):
    number = 10
    """Hidden Markov Model Regime-Switching adaptive strategy.

    The full proposal describes HMM-RS as a *meta-layer* that dynamically
    allocates across multiple underlying strategies based on latent market
    regime. The current Polyfon execution engine runs one strategy instance at
    a time, so this implementation adapts the proposal into a **self-contained
    regime-aware selector**:

    - infer a soft posterior over four latent regimes using currently-available
      online features only,
    - map the dominant regime into a regime-consistent directional rule,
    - size confidence by posterior concentration and signal strength,
    - reduce or skip trading when the inferred regime is ambiguous.

    Regimes:
        calm        -> mean reversion around the window open / fair value
        trending    -> momentum continuation with fair-value confirmation
        volatile    -> informed-flow style confirmation (displacement + spread)
        converging  -> pin-risk / theta-style fair-value mean reversion

    Features used (all available in Context without future knowledge):
        - long realized volatility
        - short/long volatility ratio
        - absolute displacement from window open
        - Polymarket YES/NO spread
        - normalized distance to strike

    This is not a full Baum-Welch-trained Gaussian HMM. Instead it is a
    lightweight online approximation suitable for the current codebase and data
    availability. It preserves the core project intent: regime-adaptive trading
    without introducing unsupported dependencies or look-ahead behavior.
    """

    name = "HMM"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    _REGIMES = ("calm", "trending", "volatile", "converging")

    def __init__(
        self,
        gamma_min: float = 0.55,
        novelty_threshold: float = 0.35,
        trend_threshold: float = 0.0008,
        vol_ratio_threshold: float = 1.35,
        spread_wide_threshold: float = 0.035,
        theta_entry: float = 0.03,
        calm_reversion_threshold: float = 0.0010,
        tau_max: float = 180.0,
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
        if not 0.0 < gamma_min < 1.0:
            raise ValueError(f"gamma_min must be in (0,1), got {gamma_min}")
        if not 0.0 < novelty_threshold < 1.0:
            raise ValueError(f"novelty_threshold must be in (0,1), got {novelty_threshold}")
        self.gamma_min = gamma_min
        self.novelty_threshold = novelty_threshold
        self.trend_threshold = trend_threshold
        self.vol_ratio_threshold = vol_ratio_threshold
        self.spread_wide_threshold = spread_wide_threshold
        self.theta_entry = theta_entry
        self.calm_reversion_threshold = calm_reversion_threshold
        self.tau_max = tau_max
        self.tau_min = tau_min
        self.replay_cadence_seconds = replay_cadence_seconds
        self.q_max = q_max
        self.theta_edge = theta_edge
        self.order_class = order_class
        self.time_in_force = time_in_force
        self.fee_rate = fee_rate

    def _resolve_order(self, price: float) -> tuple[float, float] | None:
        if price <= 0:
            return None
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

    @staticmethod
    def _sigmoid(x: float) -> float:
        return float(1.0 / (1.0 + np.exp(-x)))

    def _infer_regime(self, context: Context) -> Optional[dict[str, float]]:
        if context.spot_price is None or context.window_open_price is None:
            return None
        if context.sigma_per_minute is None or context.tau_seconds is None:
            return None

        spot = context.spot_price
        strike = context.window_open_price
        sigma_long = max(float(context.sigma_per_minute or 0.0), 1e-9)
        sigma_short = float(context.sigma_short_per_minute or sigma_long)
        vol_ratio = sigma_short / sigma_long if sigma_long > 0 else 1.0
        displacement = (spot - strike) / strike if strike > 0 else 0.0
        distance_to_strike = abs(displacement)

        spreads = []
        if context.up_best_ask is not None and context.up_best_bid is not None:
            spreads.append(max(context.up_best_ask - context.up_best_bid, 0.0))
        if context.down_best_ask is not None and context.down_best_bid is not None:
            spreads.append(max(context.down_best_ask - context.down_best_bid, 0.0))
        avg_spread = float(np.mean(spreads)) if spreads else 0.0

        calm_score = (
            2.4 * self._sigmoid((self.vol_ratio_threshold - vol_ratio) * 3.0)
            + 1.7 * self._sigmoid((self.calm_reversion_threshold - distance_to_strike) * 2500.0)
            + 1.2 * self._sigmoid((self.spread_wide_threshold - avg_spread) * 70.0)
        )
        trending_score = (
            2.8 * self._sigmoid((distance_to_strike - self.trend_threshold) * 2200.0)
            + 1.8 * self._sigmoid((vol_ratio - 1.0) * 3.0)
            + 0.8 * self._sigmoid((self.spread_wide_threshold - avg_spread) * 60.0)
        )
        volatile_score = (
            2.5 * self._sigmoid((vol_ratio - self.vol_ratio_threshold) * 4.0)
            + 1.9 * self._sigmoid((avg_spread - self.spread_wide_threshold) * 70.0)
            + 1.0 * self._sigmoid((distance_to_strike - self.trend_threshold * 0.5) * 1500.0)
        )
        converging_score = (
            2.6 * self._sigmoid((self.calm_reversion_threshold * 0.75 - distance_to_strike) * 3500.0)
            + 1.8 * self._sigmoid((120.0 - float(context.tau_seconds)) / 15.0)
            + 1.0 * self._sigmoid((avg_spread - self.spread_wide_threshold * 0.5) * 80.0)
        )

        raw = np.array([calm_score, trending_score, volatile_score, converging_score], dtype=np.float64)
        raw = raw - np.max(raw)
        probs = np.exp(raw)
        probs = probs / np.sum(probs)
        return {name: float(prob) for name, prob in zip(self._REGIMES, probs, strict=False)}

    def _market_price_for_direction(self, direction: str, context: Context) -> Optional[float]:
        if direction == "BUY_YES":
            return context.up_best_ask
        if direction == "BUY_NO":
            return context.down_best_ask
        return None

    def _build_signal(
        self,
        *,
        direction: str,
        context: Context,
        regime: str,
        regime_prob: float,
        signal_strength: float,
        metadata: dict[str, float | str],
    ) -> Optional[Signal]:
        price = self._market_price_for_direction(direction, context)
        if price is None:
            return None
        resolved = self._resolve_order(price)
        if resolved is None:
            return None
        shares, notional = resolved
        fee = taker_fee_usdc(shares, price, self.fee_rate)
        edge = signal_strength - fee / shares
        if edge <= self.theta_edge:
            return None
        confidence = min(max(signal_strength / max(self.theta_entry, 1e-9), 0.0), 1.0)
        confidence *= min(max((regime_prob - self.gamma_min) / max(1.0 - self.gamma_min, 1e-9), 0.0), 1.0)
        return Signal(
            strategy=self.name,
            direction=direction,
            size=shares,
            expected_edge=float(edge),
            confidence=float(min(confidence, 1.0)),
            metadata={
                **metadata,
                "regime": regime,
                "regime_prob": float(regime_prob),
                "order_class": self.order_class,
                "time_in_force": self.time_in_force,
                "q_max": self.q_max,
                "notional": notional,
            },
        )

    def on_tick(self, window: Any, context: Context) -> Optional[Signal]:
        if context.spot_price is None or context.window_open_price is None:
            return None
        if context.fair_probability is None or context.tau_seconds is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        regime_probs = self._infer_regime(context)
        if not regime_probs:
            return None
        regime, regime_prob = max(regime_probs.items(), key=lambda item: item[1])
        if regime_prob < self.gamma_min:
            return None
        if regime_prob < self.novelty_threshold:
            return None

        spot = context.spot_price
        strike = context.window_open_price
        displacement = (spot - strike) / strike if strike > 0 else 0.0
        fair = context.fair_probability
        up_ask = context.up_best_ask
        down_ask = context.down_best_ask
        yes_mispricing = fair - up_ask if up_ask is not None else None
        no_fair = 1.0 - fair
        no_mispricing = no_fair - down_ask if down_ask is not None else None

        meta_base = {
            "tau": float(tau),
            "spot": float(spot),
            "strike": float(strike),
            "displacement": float(displacement),
            "fair_probability": float(fair),
            **{f"posterior_{name}": float(prob) for name, prob in regime_probs.items()},
        }

        if regime == "trending":
            if displacement > self.trend_threshold and yes_mispricing is not None and yes_mispricing > self.theta_entry:
                return self._build_signal(
                    direction="BUY_YES",
                    context=context,
                    regime=regime,
                    regime_prob=regime_prob,
                    signal_strength=float(yes_mispricing),
                    metadata=meta_base,
                )
            if displacement < -self.trend_threshold and no_mispricing is not None and no_mispricing > self.theta_entry:
                return self._build_signal(
                    direction="BUY_NO",
                    context=context,
                    regime=regime,
                    regime_prob=regime_prob,
                    signal_strength=float(no_mispricing),
                    metadata=meta_base,
                )

        if regime == "volatile":
            spreads = []
            if context.up_best_ask is not None and context.up_best_bid is not None:
                spreads.append(max(context.up_best_ask - context.up_best_bid, 0.0))
            if context.down_best_ask is not None and context.down_best_bid is not None:
                spreads.append(max(context.down_best_ask - context.down_best_bid, 0.0))
            avg_spread = float(np.mean(spreads)) if spreads else 0.0
            if displacement > 0 and yes_mispricing is not None and avg_spread >= self.spread_wide_threshold and yes_mispricing > self.theta_entry:
                return self._build_signal(
                    direction="BUY_YES",
                    context=context,
                    regime=regime,
                    regime_prob=regime_prob,
                    signal_strength=float(yes_mispricing),
                    metadata={**meta_base, "avg_spread": avg_spread},
                )
            if displacement < 0 and no_mispricing is not None and avg_spread >= self.spread_wide_threshold and no_mispricing > self.theta_entry:
                return self._build_signal(
                    direction="BUY_NO",
                    context=context,
                    regime=regime,
                    regime_prob=regime_prob,
                    signal_strength=float(no_mispricing),
                    metadata={**meta_base, "avg_spread": avg_spread},
                )

        if regime == "converging":
            if yes_mispricing is not None and yes_mispricing > self.theta_entry:
                return self._build_signal(
                    direction="BUY_YES",
                    context=context,
                    regime=regime,
                    regime_prob=regime_prob,
                    signal_strength=float(yes_mispricing),
                    metadata=meta_base,
                )
            if no_mispricing is not None and no_mispricing > self.theta_entry:
                return self._build_signal(
                    direction="BUY_NO",
                    context=context,
                    regime=regime,
                    regime_prob=regime_prob,
                    signal_strength=float(no_mispricing),
                    metadata=meta_base,
                )

        if regime == "calm":
            if displacement > self.calm_reversion_threshold and no_mispricing is not None and no_mispricing > self.theta_entry:
                return self._build_signal(
                    direction="BUY_NO",
                    context=context,
                    regime=regime,
                    regime_prob=regime_prob,
                    signal_strength=float(no_mispricing),
                    metadata=meta_base,
                )
            if displacement < -self.calm_reversion_threshold and yes_mispricing is not None and yes_mispricing > self.theta_entry:
                return self._build_signal(
                    direction="BUY_YES",
                    context=context,
                    regime=regime,
                    regime_prob=regime_prob,
                    signal_strength=float(yes_mispricing),
                    metadata=meta_base,
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