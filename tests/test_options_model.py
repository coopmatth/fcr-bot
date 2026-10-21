"""Unit tests for the Black-Scholes pricer (fcr/options_model.py).

These test the MODEL's internal consistency only — put-call parity,
the intrinsic floor, expiry behavior. A passing suite says nothing
about whether modeled premiums match real 0DTE quotes; that error
is structural and is handled by reporting a band across IV anchors.
"""
import math

import pytest

from fcr.options_model import (
    TRADING_MINUTES_PER_YEAR,
    bs_price,
    intrinsic_value,
)


def test_put_call_parity_synthetic():
    # C - P = S - K*exp(-rT) (no dividends), across moneyness/expiry.
    r = 0.043
    for spot, strike in [(100.0, 100.0), (120.0, 100.0), (80.0, 100.0),
                         (773.07, 773.0), (500.0, 525.0)]:
        for minutes in [16.0, 240.0, 390.0, 5 * 390.0]:
            for sigma in [0.10, 0.20, 0.45]:
                c = bs_price(spot, strike, minutes, sigma, "call", r=r)
                p = bs_price(spot, strike, minutes, sigma, "put", r=r)
                t = minutes / TRADING_MINUTES_PER_YEAR
                forward_diff = spot - strike * math.exp(-r * t)
                # The pricer floors at intrinsic value (a frozen model
                # convention). A deep-ITM European put's raw BS value
                # sits BELOW intrinsic by up to the strike's discount,
                # K*(1-exp(-rT)), so the floor bends parity by at
                # most that amount. Allow exactly that much.
                tol = 0.02 + strike * (1.0 - math.exp(-r * t))
                assert c - p == pytest.approx(forward_diff, abs=tol)


def test_intrinsic_floor_binds():
    # Deep ITM with tiny vol and minutes left: worth ~intrinsic.
    assert bs_price(130.0, 100.0, 5.0, 0.05, "call") >= 30.0
    assert bs_price(70.0, 100.0, 5.0, 0.05, "put") >= 30.0
    # Deep OTM: worth ~0, never negative.
    assert bs_price(70.0, 100.0, 5.0, 0.05, "call") == pytest.approx(0.0, abs=0.01)
    assert bs_price(130.0, 100.0, 5.0, 0.05, "put") == pytest.approx(0.0, abs=0.01)


def test_intrinsic_value():
    assert intrinsic_value(110.0, 100.0, "call") == 10.0
    assert intrinsic_value(90.0, 100.0, "call") == 0.0
    assert intrinsic_value(90.0, 100.0, "put") == 10.0
    assert intrinsic_value(110.0, 100.0, "put") == 0.0


def test_near_expiry_behavior():
    # ATM with 1 minute left: nearly worthless but non-negative.
    v = bs_price(100.0, 100.0, 1.0, 0.20, "call")
    assert 0.0 <= v < 0.05
    # Time floor: 0 minutes prices identically to 1 minute.
    assert bs_price(100.0, 100.0, 0.0, 0.20, "call") == v
    # ATM value grows with time and with vol (theta/vega sanity).
    assert bs_price(100.0, 100.0, 240.0, 0.20, "call") > v
    assert bs_price(100.0, 100.0, 240.0, 0.40, "call") \
        > bs_price(100.0, 100.0, 240.0, 0.20, "call")


def test_atm_plausible_range():
    # A full session to expiry at 15% vol: ATM SPY-like premium ~$1.5.
    v = bs_price(600.0, 600.0, 390.0, 0.15, "call")
    assert 1.0 < v < 2.5


def test_bad_kind_rejected():
    with pytest.raises(ValueError):
        bs_price(100.0, 100.0, 60.0, 0.2, "straddle")
    with pytest.raises(ValueError):
        intrinsic_value(100.0, 100.0, "straddle")
