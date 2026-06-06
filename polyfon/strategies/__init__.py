"""Strategy modules."""
from polyfon.strategies.base import BaseStrategy, Context, Signal, StrategyRegistry, register
from polyfon.strategies.sla import SLAStrategy
from polyfon.strategies.wdm import WDMStrategy
from polyfon.strategies.tde import TDEStrategy
from polyfon.strategies.rom import ROMStrategy
from polyfon.strategies.pmr import PMRStrategy
from polyfon.strategies.obi import OBIStrategy
from polyfon.strategies.mpr import MPRStrategy
from polyfon.strategies.vit import VITStrategy
from polyfon.strategies.crv import CRVStrategy
from polyfon.strategies.cll import CLLStrategy
from polyfon.strategies.vpx import VPXStrategy
from polyfon.strategies.hmm import HMMStrategy
from polyfon.strategies.mip import MIPStrategy

__all__ = ["BaseStrategy", "Context", "Signal", "StrategyRegistry", "register", "SLAStrategy", "WDMStrategy", "TDEStrategy", "ROMStrategy", "PMRStrategy", "OBIStrategy", "MPRStrategy", "VITStrategy", "CRVStrategy", "CLLStrategy", "VPXStrategy", "HMMStrategy", "MIPStrategy"]
