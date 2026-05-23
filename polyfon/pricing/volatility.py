"""Volatility estimation from spot returns."""
from collections import deque
from typing import Optional
import math


class RollingVolatility:
    """Compute rolling realized volatility from log returns.

    Uses a fixed-size window of recent prices and computes
    the standard deviation of log returns scaled per minute.
    """

    def __init__(self, window: int = 60, interval_sec: float = 1.0):
        """
        Args:
            window: Number of observations in the rolling window.
            interval_sec: Time between observations in seconds.
        """
        self._prices: deque[float] = deque(maxlen=window + 1)
        self._window = window
        self._interval_sec = interval_sec

    def update(self, price: float, timestamp: Optional[float] = None) -> None:
        """Add a new price observation."""
        self._prices.append(price)

    @property
    def sigma_per_minute(self) -> Optional[float]:
        """Realized volatility per minute, or None if insufficient data."""
        if len(self._prices) < 3:
            return None
        prices = list(self._prices)
        log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        sigma_per_interval = math.sqrt(variance)
        # Annualize or scale to per-minute
        intervals_per_minute = 60.0 / self._interval_sec
        return sigma_per_interval * math.sqrt(intervals_per_minute)

    @property
    def sigma_per_second(self) -> Optional[float]:
        """Realized volatility per second."""
        s = self.sigma_per_minute
        if s is None:
            return None
        return s / math.sqrt(60.0)
