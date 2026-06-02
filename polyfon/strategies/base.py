"""Base strategy interface and registry."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Signal:
    """Trading signal produced by a strategy."""
    strategy: str
    direction: str  # BUY_YES or BUY_NO for Polymarket entry simulation
    size: float
    expected_edge: float
    confidence: float = 1.0
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class Context:
    """Execution context passed to strategies."""
    spot_price: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    fair_probability: Optional[float] = None
    tau_seconds: Optional[float] = None
    sigma_per_minute: Optional[float] = None
    timestamp: Optional[datetime] = None
    window_open_price: Optional[float] = None

    # Intra-window spot range (for range-break strategies like ROM).
    range_high: Optional[float] = None
    range_low: Optional[float] = None

    # Intra-window mean spot price (for mean-reversion strategies like MPR).
    mean_spot_price: Optional[float] = None

    # Token-specific book prices (resolves the UP/DOWN ambiguity).
    up_best_bid: Optional[float] = None
    up_best_ask: Optional[float] = None
    down_best_bid: Optional[float] = None
    down_best_ask: Optional[float] = None

    # Token-specific book sizes (for order-book imbalance strategies).
    up_bid_size: Optional[float] = None
    up_ask_size: Optional[float] = None
    down_bid_size: Optional[float] = None
    down_ask_size: Optional[float] = None

    # Cross-asset lead-lag (for CLL strategy).
    leader_spot_price: Optional[float] = None
    leader_return: Optional[float] = None


@dataclass
class ReplayPlan:
    """Strategy-owned historical replay schedule for one window."""

    eval_times: List[datetime] = field(default_factory=list)
    stop_on_signal: bool = True
    cadence_seconds: float = 1.0


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    name: str = "base"

    @abstractmethod
    def on_tick(self, window: Any, context: Context) -> Optional[Signal]:
        """Called on every tick/update while window is open."""
        ...

    @abstractmethod
    def on_window_close(self, window: Any, context: Context) -> Optional[Signal]:
        """Called when a window closes (just before resolution)."""
        ...

    def build_replay_plan(self, window: Any) -> ReplayPlan:
        """Return the historical evaluation schedule for this strategy/window.

        Default behavior preserves the previous dry-mode semantics: evaluate
        once at T-10s.
        """
        return ReplayPlan(eval_times=[window.end_et - timedelta(seconds=10)])


class StrategyRegistry:
    """Registry of all available strategies."""

    _strategies: Dict[str, type[BaseStrategy]] = {}

    @classmethod
    def add(cls, strategy_class: type[BaseStrategy]) -> type[BaseStrategy]:
        cls._strategies[strategy_class.name] = strategy_class
        return strategy_class

    @classmethod
    def get(cls, name: str) -> Optional[type[BaseStrategy]]:
        return cls._strategies.get(name)

    @classmethod
    def list_strategies(cls) -> List[str]:
        return list(cls._strategies.keys())

    @classmethod
    def instantiate(cls, name: str, **kwargs: Any) -> Optional[BaseStrategy]:
        strat_class = cls._strategies.get(name)
        if strat_class is None:
            return None
        return strat_class(**kwargs)

    @classmethod
    def list_strategies_with_dates(cls) -> List[tuple[str, datetime]]:
        """Return (name, registration_date) sorted oldest → newest.

        The registration date is the first time the module defining the
        strategy class was imported.  For strategies that pre-date this
        attribute the default is the current UTC timestamp at import time.
        """
        return sorted(
            (
                (name, getattr(klass, "_registered_at", datetime.utcnow()))
                for name, klass in cls._strategies.items()
            ),
            key=lambda x: x[1],
        )


def register(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    """Decorator to register a strategy class."""
    cls._registered_at = datetime.utcnow()
    return StrategyRegistry.add(cls)
