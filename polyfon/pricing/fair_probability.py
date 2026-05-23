"""Fair probability calculation for binary prediction markets."""
import math
from typing import Optional

from scipy.stats import norm

from polyfon.pricing.volatility import RollingVolatility


def fair_probability(
    spot: float,
    strike: float,
    tau_seconds: float,
    sigma_per_minute: float,
    drift: float = 0.0,
) -> float:
    """Compute fair probability that spot > strike at resolution.

    Uses a Black-Scholes-style binary call approximation:
        pi_hat = Phi( d1 )
    where d1 = (ln(S/K) + (mu - sigma^2/2) * tau) / (sigma * sqrt(tau))

    Args:
        spot: Current spot price.
        strike: Contract strike / threshold.
        tau_seconds: Time to resolution in seconds.
        sigma_per_minute: Estimated volatility per minute.
        drift: Drift parameter (default 0 for pure diffusion over 5 min).

    Returns:
        Fair probability in [0, 1].
    """
    if tau_seconds <= 0:
        return 1.0 if spot > strike else 0.0

    # Convert sigma to per-second basis
    tau_minutes = tau_seconds / 60.0
    sigma = sigma_per_minute * math.sqrt(tau_minutes)
    if sigma == 0:
        return 1.0 if spot > strike else 0.0

    d1 = (math.log(spot / strike) + (drift - 0.5 * sigma_per_minute ** 2) * tau_minutes) / sigma
    return float(norm.cdf(d1))
