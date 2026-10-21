"""Black-Scholes pricer for the MODELED ORB-15 options backtest.

Stdlib only (math.erf). This is a MODEL, not market data: premiums
are estimated from the underlying tape with an assumed volatility,
and the error concentrates exactly where 0DTE trades live (minutes
to expiry, breakout-day IV). Every consumer of these prices must
present results as a BAND across IV anchors, never a single number.

Conventions (frozen for the modeled run):
  * risk-free rate r = 0.043 flat, no dividend adjustment
  * time is measured in TRADING minutes: 1 year = 252 x 390 minutes,
    consistent with the realized-vol anchor's annualization
  * time-to-expiry is floored at 1 minute
  * the returned mid is floored at intrinsic value
"""
from __future__ import annotations

import math

TRADING_MINUTES_PER_YEAR = 252 * 390
RISK_FREE_RATE = 0.043
MIN_MINUTES_TO_EXPIRY = 1.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def intrinsic_value(spot: float, strike: float, kind: str) -> float:
    if kind == "call":
        return max(spot - strike, 0.0)
    if kind == "put":
        return max(strike - spot, 0.0)
    raise ValueError(f"unknown option kind: {kind!r}")


def bs_price(spot: float, strike: float, minutes_to_expiry: float,
             sigma: float, kind: str,
             r: float = RISK_FREE_RATE) -> float:
    """Black-Scholes mid price per share for a European call/put.

    minutes_to_expiry is in trading minutes (see module docstring);
    sigma is the annualized volatility as a fraction (0.20 = 20%).
    The result is floored at intrinsic value.
    """
    if kind not in ("call", "put"):
        raise ValueError(f"unknown option kind: {kind!r}")
    intrinsic = intrinsic_value(spot, strike, kind)
    t = max(float(minutes_to_expiry), MIN_MINUTES_TO_EXPIRY) \
        / TRADING_MINUTES_PER_YEAR
    if sigma <= 0:
        return intrinsic
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike)
          + (r + 0.5 * sigma * sigma) * t) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    disc = math.exp(-r * t)
    if kind == "call":
        price = spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    else:
        price = strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(price, intrinsic)
