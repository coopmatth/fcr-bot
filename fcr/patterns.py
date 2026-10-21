"""Pattern detectors for the First Candle Rule strategy.

Pure functions on Bar tuples so they can be unit-tested in isolation and
reused by both the backtester and a future live-paper runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class FVG:
    """A fair value gap: the price zone skipped by a displacement move.

    direction: 'bullish' (gap up, supports a long) or 'bearish' (gap down,
    supports a short). bottom/top bound the gap zone.
    """

    direction: str
    bottom: float
    top: float


def detect_fvg(b0: Bar, b1: Bar, b2: Bar) -> Optional[FVG]:
    """Detect a fair value gap across three consecutive 1-minute bars.

    Bullish FVG: b2.low > b0.high  -> gap zone is (b0.high, b2.low).
    Bearish FVG: b2.high < b0.low  -> gap zone is (b2.high, b0.low).
    b1 is the displacement candle; it is part of the signature for clarity
    even though the gap test only needs b0 and b2.
    """
    _ = b1  # displacement candle; gap is defined by the outer bars' wicks
    if b2.low > b0.high:
        return FVG(direction="bullish", bottom=b0.high, top=b2.low)
    if b2.high < b0.low:
        return FVG(direction="bearish", bottom=b2.high, top=b0.low)
    return None


def is_bullish_engulfing(prev: Bar, cur: Bar) -> bool:
    """Bullish engulfing: prev is bearish, cur is bullish, and cur's body
    fully covers prev's body (cur.open <= prev.close and cur.close >= prev.open)."""
    if not (prev.close < prev.open):  # prev must be a down candle
        return False
    if not (cur.close > cur.open):  # cur must be an up candle
        return False
    return cur.open <= prev.close and cur.close >= prev.open


def is_bearish_engulfing(prev: Bar, cur: Bar) -> bool:
    """Bearish engulfing: mirror of bullish (prev bullish, cur bearish,
    cur.open >= prev.close and cur.close <= prev.open)."""
    if not (prev.close > prev.open):  # prev must be an up candle
        return False
    if not (cur.close < cur.open):  # cur must be a down candle
        return False
    return cur.open >= prev.close and cur.close <= prev.open
