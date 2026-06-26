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

    The GPD input is the BS-style standardised moneyness
    ``d1 = ln(S/K) / (sigma * sqrt(tau))`` — the same variable used by
    the fair-probability engine.  When ``|d1| > u_sigma`` the GPD tail
    model replaces the normal CDF, providing fatter-tailed probabilities
    that better match realised crypto returns.

    Key insight: at a market price of 0.01 the Polymarket taker fee is
    ~6.93 % of notional, so the true probability must exceed ~7 % just to
    break even.  The strategy therefore requires a minimum market price
    (``min_market_price``) to avoid penny contracts where fees dominate.

    Parameters (empirically discoverable):
        xi:              GPD tail index (shape).  Higher = fatter tails.
                         Crypto typically 0.15-0.30 (default 0.22).
        sigma_gpd:       GPD scale parameter (default 0.85).
        u_sigma:         POT threshold in units of d1.  Must exceed ~2.0
                         so that exceedances are genuine tail events and
                         not routine 5-minute noise (default 2.5).
        lambda_u:        Unconditional exceedance probability
                         P(|d1| > u_sigma).  Estimated from historical
                         5-minute return distributions.  For BTC at u=2.5σ
                         this is typically 3-8 % (default 0.05).
        m_tail:          Minimum EVT/BS ratio to consider trading (default 2.0).
        theta_entry:     Minimum |mispricing| between EVT probability and
                         market price to enter (default 0.01).
        min_market_price Minimum market price to trade.  Contracts priced
                         below this are skipped because taker fees dominate
                         the edge (default 0.02).
        tau_max:         Earliest entry before resolution (default 300 s).
        tau_min:         Latest entry before resolution (default 15 s).
        q_max:           Position size (USDC for market orders).
        theta_edge:      Minimum expected edge per share to trade.
        order_class:     'market' (default) or 'limit'.
        time_in_force:   'FOK' (default for market), 'FAK', 'GTC', 'GTD'.
        fee_rate:        Market taker fee rate.

    Edge source:
        Polymarket systematically underestimates the probability of extreme
        spot moves (>2.5 sigma) at short horizons due to lognormal pricing.
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
        u_sigma: float = 2.5,
        lambda_u: float = 0.10,
        m_tail: float = 2.0,
        theta_entry: float = 0.01,
        min_market_price: float = 0.02,
        tau_max: float = 300.0,
        tau_min: float = 15.0,
        replay_cadence_seconds: float = 5.0,
        q_max: float = 1.0,
        theta_edge: float = 0.005,
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
        if not (0 < min_market_price < 1):
            raise ValueError(f"min_market_price must be in (0, 1), got {min_market_price}")

        self.xi = xi
        self.sigma_gpd = sigma_gpd
        self.u_sigma = u_sigma
        self.lambda_u = lambda_u
        self.m_tail = m_tail
        self.theta_entry = theta_entry
        self.min_market_price = min_market_price
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

        The GPD input is the BS-style standardised moneyness
        ``|d1| = |ln(S/K)| / (sigma * sqrt(tau))``.  When |d1| exceeds
        the POT threshold ``u_sigma``, the GPD survival function replaces
        the normal CDF tail, giving fatter-tailed probabilities that
        better match realised crypto returns.

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

        # d1 without drift — standardised distance in total-vol units
        d1 = math.log(spot / strike) / sigma_total

        # BS probability (baseline and fallback)
        bs_prob = float(norm.cdf(d1))

        # Absolute distance in sigma units
        abs_d1 = abs(d1)

        if abs_d1 < self.u_sigma:
            return bs_prob

        # GPD survival: S(y) = (1 + xi * y / sigma_gpd)^{-1/xi}
        y = abs_d1 - self.u_sigma
        arg = 1.0 + self.xi * y / self.sigma_gpd
        if arg <= 0:
            return bs_prob

        gpd_survival = arg ** (-1.0 / self.xi)

        # EVT tail probability: lambda_u * S(y)
        evt_tail = self.lambda_u * gpd_survival
        evt_tail = max(0.001, min(0.999, evt_tail))

        if d1 < 0:
            # K > S → YES is DOTM → right-tail → P(S_T > K)
            return evt_tail
        else:
            # K < S → NO is DOTM → left-tail → P(S_T > K) = 1 - P(S_T < K)
            return 1.0 - evt_tail

    def _generate_signal(
        self,
        direction: str,
        price: float,
        evt_prob: float,
        bs_prob: float,
        mispricing: float,
        tau: float,
        abs_d1: float,
    ) -> Optional[Signal]:
        """Common signal generation for both BUY_YES and BUY_NO."""
        resolved = self._resolve_order(price)
        if resolved is None:
            return None
        shares, notional = resolved
        fee = taker_fee_usdc(shares, price, self.fee_rate)
        edge = mispricing - fee / shares
        if edge < self.theta_edge:
            return None

        # Confidence: product of (1) how much EVT exceeds BS, and (2) how
        # deep in the tail we are.  Both factors clamped to [0, 1].
        bs_ref = max(bs_prob, 1e-10)
        evt_bs_ratio = evt_prob / bs_ref
        ratio_excess = (evt_bs_ratio - self.m_tail) / max(self.m_tail, 1e-6)
        factor_ratio = min(max(ratio_excess, 0.0), 1.0)
        depth = (abs_d1 - self.u_sigma) / self.u_sigma
        factor_depth = min(max(depth, 0.0), 1.0)
        confidence = factor_ratio * factor_depth

        return Signal(
            strategy=self.name,
            direction=direction,
            size=shares,
            expected_edge=float(edge),
            confidence=float(max(0.0, min(confidence, 1.0))),
            metadata={
                "evt_prob": float(evt_prob),
                "bs_prob": float(bs_prob),
                "evt_bs_ratio": float(evt_bs_ratio),
                "mispricing": float(mispricing),
                "tau": tau,
                "abs_d1": float(abs_d1),
                "order_class": self.order_class,
                "time_in_force": self.time_in_force,
                "q_max": self.q_max,
                "notional": notional,
            },
        )

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

        # Standardised moneyness (same as in _evt_binary_probability)
        tau_min = tau / 60.0
        sigma_total = sigma * math.sqrt(tau_min)
        if sigma_total <= 0:
            return None
        d1 = math.log(spot / strike) / sigma_total
        abs_d1 = abs(d1)

        if abs_d1 < self.u_sigma:
            return None

        if d1 < 0:
            # K > S → YES is DOTM
            if evt_prob < self.m_tail * bs_prob:
                return None
            if context.up_best_ask is None:
                return None
            price = context.up_best_ask
            if price < self.min_market_price:
                return None
            mispricing = evt_prob - price
            if mispricing < self.theta_entry:
                return None
            return self._generate_signal(
                "BUY_YES", price, evt_prob, bs_prob, mispricing, tau, abs_d1,
            )
        else:
            # K < S → NO is DOTM
            evt_no_prob = 1.0 - evt_prob
            bs_no_prob = 1.0 - bs_prob
            if evt_no_prob < self.m_tail * bs_no_prob:
                return None
            if context.down_best_ask is None:
                return None
            price = context.down_best_ask
            if price < self.min_market_price:
                return None
            mispricing = evt_no_prob - price
            if mispricing < self.theta_entry:
                return None
            return self._generate_signal(
                "BUY_NO", price, evt_no_prob, bs_no_prob, mispricing, tau, abs_d1,
            )

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
