"""Strategy modules."""
from polyfon.strategies.base import BaseStrategy, Context, Signal, StrategyRegistry, register
from polyfon.strategies.sla import SLAStrategy
from polyfon.strategies.wdm import WDMStrategy
from polyfon.strategies.tde import TDEStrategy
from polyfon.strategies.rom import ROMStrategy
from polyfon.strategies.pmr import PMRStrategy

__all__ = ["BaseStrategy", "Context", "Signal", "StrategyRegistry", "register", "SLAStrategy", "WDMStrategy", "TDEStrategy", "ROMStrategy", "PMRStrategy"]
