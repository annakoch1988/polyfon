"""Strategy modules."""
from polyfon.strategies.base import BaseStrategy, Context, Signal, StrategyRegistry, register
from polyfon.strategies.sla import SLAStrategy

__all__ = ["BaseStrategy", "Context", "Signal", "StrategyRegistry", "register", "SLAStrategy"]
