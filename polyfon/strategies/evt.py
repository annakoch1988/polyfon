"""Strategy: Extreme-Value Tail Harvesting (EVT)."""
import math
from datetime import timedelta
from typing import Any, Optional

from scipy.stats import norm

from polyfon.strategies.base import BaseStrategy, Context, ReplayPlan, Signal, register
from polyfon.utils.fees import taker_fee_usdc


@register
class EVTStrategy(BaseStrategy):
    number = 18
    """Extreme-Value Tail Harvesting.

    Corrects the systematic underpricing of deep out-of-the-money (DOTM)
    Polymarket contracts caused by the lognormal (Black-Scholes) pricing
    assumption.  Crypto returns exhibit power-law tails with tail index
    xi ~ 0.15-0.30, meaning extreme moves occur orders of magnitude more
    frequently than a normal distribution predicts.

    Uses the Peak Over Threshold (POT) method with a Generalized Pareto
    Distribution (GPD) to estimate tail probabilities that are substantially
    higher than BS-implied values for extreme strikes.  Buys DOTM contracts
    when the EVT probability substantially exceeds both the BS probability
    and the market price, capturing a lottery-ticket-style positive expected
    value.

    Parameters (empirically discoverable):
        xi:           GPD tail index (shape parameter).  Higher = fatter tails.
                      Crypto typically 0.15-0.30 (default 0.22).
        sigma_gpd:    GPD scale parameter (default 0.85).
        u_sigma:      POT threshold in units of sigma (default 1.65).
                      Mean exceedance probability lambda_u is derived from
                      this assuming a standard normal threshold exceedance.
        lambda_u:     Unconditional exceedance probability P(|r| > u).
                      For normal distribution at u=1.65sigma, ~0.10.
                      Crypto fat tails push this higher (default 0.10).
        m_tail:       Minimum ratio EVT_prob / BS_prob to consider trading
                      a DOTM contract (default 2.5).
        theta_entry:  Minimum |mispricing| between EVT probability and market
                      price to enter (default 0.02).
        tau_max:      Earliest entry before resolution (default 300s).
        tau_min:      Latest entry before resolution (default 15s).
        q_max:        Position size (USDC for market orders).
        theta_edge:   Minimum expected edge per share to trade.
        order_class:  'market' (default) or 'limit'.
        time_in_force: 'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
        fee_rate:     Market taker fee rate.

    Edge source:
        Polymarket systematically underestimates the probability of extreme
        spot moves (>3 sigma) at short horizons due to lognormal pricing.
        Human traders anchor on recent normal times and neglect tail risk.
        MMs price based on recent volatility without tail calibration.
        Crypto leverage cascades, exchange shocks, and regulatory news
        generate fatter tails than lognormal predicts.
    """

    name = "EVT"

    _MARKET_TIF = frozenset({"FOK", "FAK"})
    _LIMIT_TIF = frozenset({"GTC", "GTD"})

    def __init__(
        self,
        xi: float = 0.22,
        sigma_gpd: float = 0.85,
        u_sigma: float = 1.65,
        lambda_u: float = 0.10,
        m_tail: float = 2.5,
        theta_entry: float = 0.02,
        tau_max: float = 300.0,
        tau_min: float = 15.0,
        replay_cadence_seconds: float = 5.0,
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
        if xi <= 0:
            raise ValueError(f"xi must be > 0 (fat tails), got {xi}")
        if sigma_gpd <= 0:
            raise ValueError(f"sigma_gpd must be > 0, got {sigma_gpd}")
        if u_sigma <= 0:
            raise ValueError(f"u_sigma must be > 0, got {u_sigma}")
        if not (0 < lambda_u < 1):
            raise ValueError(f"lambda_u must be in (0, 1), got {lambda_u}")
        if m_tail < 1.0:
            raise ValueError(f"m_tail must be >= 1.0, got {m_tail}")

        self.xi = xi
        self.sigma_gpd = sigma_gpd
        self.u_sigma = u_sigma
        self.lambda_u = lambda_u
        self.m_tail = m_tail
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

    def _evt_binary_probability(
        self, spot: float, strike: float, tau: float, sigma: float
    ) -> float:
        """Compute EVT probability that S_T > K using POT/GPD.

        For deep OTM contracts, the GPD tail probability is substantially
        higher than the BS lognormal probability.  For deep ITM contracts
        (opposite tail), the correction applies symmetrically.

        Returns:
            Probability in [0.001, 0.999].
        """
        if tau <= 0:
            return 1.0 if spot > strike else 0.0
        if sigma <= 0:
            return 1.0 if spot > strike else 0.0

        tau_min = tau / 60.0
        sigma_total = sigma * math.sqrt(tau_min)
        if sigma_total <= 0:
            return 1.0 if spot > strike else 0.0

        # BS probability (baseline and fallback)
        d1 = math.log(spot / strike) / sigma_total
        bs_prob = float(norm.cdf(d1))

        # Distance from ATM in sigma units
        sigma_distance = abs(d1)

        if sigma_distance < self.u_sigma:
            # Not deep OTM enough for EVT to diverge materially from BS
            return bs_prob

        # GPD survival function: S(y) = (1 + xi * y / sigma_gpd)^{-1/xi}
        # where y = sigma_distance - u_sigma (excess over threshold)
        y = sigma_distance - self.u_sigma
        arg = 1.0 + self.xi * y / self.sigma_gpd
        if arg <= 0:
            return bs_prob  # Numerical breakdown, fall back to BS

        gpd_survival = arg ** (-1.0 / self.xi)

        # EVT tail probability: lambda_u * S(y)
        evt_tail_prob = self.lambda_u * gpd_survival
        evt_tail_prob = max(0.001, min(0.999, evt_tail_prob))

        if d1 < 0:
            # YES is DOTM (K >> S): right-tail correction
            return evt_tail_prob
        else:
            # NO is DOTM (K << S): left-tail correction
            # P(S_T > K) = 1 - P(S_T < K)
            return 1.0 - evt_tail_prob

    def on_tick(self, window: Any, context: Context) -> Optional[Signal]:
        if context.spot_price is None or context.window_open_price is None:
            return None
        if context.tau_seconds is None or context.sigma_per_minute is None:
            return None

        tau = context.tau_seconds
        if tau < self.tau_min or tau > self.tau_max:
            return None

        spot = context.spot_price
        strike = context.window_open_price
        sigma = context.sigma_per_minute

        if sigma <= 0:
            return None

        # EVT-adjusted fair probability
        evt_prob = self._evt_binary_probability(spot, strike, tau, sigma)

        # BS fair probability for comparison
        bs_prob = context.fair_probability
        if bs_prob is None:
            from polyfon.pricing.fair_probability import fair_probability
            bs_prob = fair_probability(spot, strike, tau, sigma)

        # Determine which side is DOTM and whether EVT justifies a trade
        tau_min = tau / 60.0
        sigma_total = sigma * math.sqrt(tau_min)
        if sigma_total <= 0:
            return None

        d1 = math.log(spot / strike) / sigma_total

        if d1 < -self.u_sigma:
            # YES is DOTM (strike >> spot)
            # EVT right-tail probability should be >> BS probability
            if evt_prob < self.m_tail * bs_prob:
                return None

            if context.up_best_ask is None:
                return None
            price = context.up_best_ask
            mispricing = evt_prob - price
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

            # Confidence scales with how extreme the tail is
            tail_extremity = min((d1 + self.u_sigma) / self.u_sigma, 1.0)
            ratio = evt_prob / max(bs_prob, 1e-10) - 1.0
            m_tail_margin = max(self.m_tail - 1.0, 1e-6)
            confidence = min(ratio / m_tail_margin, 1.0) * min(abs(tail_extremity), 1.0)

            return Signal(
                strategy=self.name,
                direction="BUY_YES",
                size=shares,
                expected_edge=float(edge),
                confidence=float(max(0.0, min(confidence, 1.0))),
                metadata={
                    "evt_prob": float(evt_prob),
                    "bs_prob": float(bs_prob),
                    "evt_bs_ratio": float(evt_prob / max(bs_prob, 1e-10)),
                    "mispricing": float(mispricing),
                    "tau": tau,
                    "d1": float(d1),
                    "order_class": self.order_class,
                    "time_in_force": self.time_in_force,
                    "q_max": self.q_max,
                    "notional": notional,
                },
            )

        elif d1 > self.u_sigma:
            # NO is DOTM (strike << spot)
            # EVT left-tail probability should be >> BS probability for NO
            evt_no_prob = 1.0 - evt_prob
            bs_no_prob = 1.0 - bs_prob
            if evt_no_prob < self.m_tail * bs_no_prob:
                return None

            if context.down_best_ask is None:
                return None
            price = context.down_best_ask
            mispricing = evt_no_prob - price
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

            tail_extremity = min((d1 - self.u_sigma) / self.u_sigma, 1.0)
            ratio = evt_no_prob / max(bs_no_prob, 1e-10) - 1.0
            m_tail_margin = max(self.m_tail - 1.0, 1e-6)
            confidence = min(ratio / m_tail_margin, 1.0) * min(abs(tail_extremity), 1.0)

            return Signal(
                strategy=self.name,
                direction="BUY_NO",
                size=shares,
                expected_edge=float(edge),
                confidence=float(max(0.0, min(confidence, 1.0))),
                metadata={
                    "evt_prob": float(evt_prob),
                    "evt_no_prob": float(evt_no_prob),
                    "bs_prob": float(bs_prob),
                    "bs_no_prob": float(bs_no_prob),
                    "evt_bs_ratio": float(evt_no_prob / max(bs_no_prob, 1e-10)),
                    "mispricing": float(mispricing),
                    "tau": tau,
                    "d1": float(d1),
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
