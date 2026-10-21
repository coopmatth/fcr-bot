"""First Candle Rule (FCR) paper-trading package. Paper simulation only."""

from .patterns import Bar, FVG, detect_fvg, is_bearish_engulfing, is_bullish_engulfing
from .engine import FCREngine, Trade, compute_target, position_size

__all__ = [
    "Bar",
    "FVG",
    "detect_fvg",
    "is_bullish_engulfing",
    "is_bearish_engulfing",
    "FCREngine",
    "Trade",
    "compute_target",
    "position_size",
]
