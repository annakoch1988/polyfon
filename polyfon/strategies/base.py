"""Base strategy interface and registry."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Any


@dataclass
class Signal:
    """Trading signal produced by a strategy."""
    strategy: str
    direction: str  # BUY_YES, SELL_YES, BUY_NO, SELL_NO
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

    # Token-specific book prices (resolves the UP/DOWN ambiguity).
    up_best_bid: Optional[float] = None
    up_best_ask: Optional[float] = None
    down_best_bid: Optional[float] = None
    down_best_ask: Optional[float] = None


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


def register(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    """Decorator to register a strategy class."""
    return StrategyRegistry.add(cls)
