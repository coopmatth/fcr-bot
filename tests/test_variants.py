"""Tests for the experiment-matrix variants (V1-V5 engine options).

V0 (no variant keys in the config) is covered by tests/test_fcr.py; the
variant plumbing must leave that behavior untouched. Each variant here is
tested at the engine level with scripted bars, mirroring test_fcr.py.

Run from the project root:  python3 -m pytest tests/ -q
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fcr.config import load_config
from fcr.engine import DONE, IN_TRADE, FCREngine
from fcr.patterns import Bar

ET = ZoneInfo("America/New_York")
BASE = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
DAY = "2026-10-20"


def bar(hhmm: str, o: float, h: float, l: float, c: float) -> Bar:
    hh, mm = map(int, hhmm.split(":"))
    return Bar(ts=datetime(2026, 10, 20, hh, mm, tzinfo=ET), open=o, high=h, low=l, close=c)


def make_engine(**overrides) -> FCREngine:
    engine = FCREngine(dict(BASE, **overrides))
    engine.start_day(DAY)
    return engine


def feed(engine: FCREngine, bars) -> None:
    for b in bars:
        engine.on_bar(b)


# Same scripted days as tests/test_fcr.py. First candle (09:30-09:34):
# high 101.0, low 99.0, body UP (first open 100.0 -> last close 100.3).
FIRST_CANDLE = [
    bar("09:30", 100.0, 100.6, 99.4, 100.2),
    bar("09:31", 100.2, 101.0, 99.8, 100.5),
    bar("09:32", 100.5, 100.8, 99.0, 100.1),
    bar("09:33", 100.1, 100.7, 99.6, 100.4),
    bar("09:34", 100.4, 100.9, 100.0, 100.3),
]

# Bullish break + FVG [100.5, 101.2] (height 0.7), retest, engulfing entry
# at 09:40 close 101.5, stop 100.5 (risk 1.0 pt), target 104.5, 5 contracts.
LONG_SETUP = FIRST_CANDLE + [
    bar("09:35", 100.2, 100.5, 99.9, 100.3),
    bar("09:36", 100.3, 101.9, 100.3, 101.7),
    bar("09:37", 101.7, 102.0, 101.2, 101.8),
    bar("09:38", 101.8, 101.9, 101.3, 101.4),
    bar("09:39", 101.4, 101.45, 100.9, 101.0),
    bar("09:40", 100.95, 101.6, 100.9, 101.5),
]

# Bearish mirror: FVG [98.6, 99.5], entry short at 09:40 close 98.0,
# stop 99.5, target 93.5.
SHORT_SETUP = FIRST_CANDLE + [
    bar("09:35", 99.8, 100.1, 99.5, 99.7),
    bar("09:36", 99.7, 99.8, 97.9, 98.2),
    bar("09:37", 98.2, 98.6, 97.9, 98.3),
    bar("09:38", 98.3, 98.5, 97.8, 98.0),
    bar("09:39", 98.0, 98.9, 97.95, 98.7),
    bar("09:40", 98.75, 98.8, 97.9, 98.0),
]


# ------------------------------------------------------------ V1: min FVG
def test_min_fvg_passes_sufficient_gap():
    # LONG_SETUP: ATR(14) over the bars before the 09:36 displacement
    # candle = mean TR of 09:30..09:35 ~ 1.13 -> threshold ~0.57; the
    # 0.7-pt FVG clears it, so the trade is taken exactly as baseline.
    engine = make_engine(min_fvg_atr_mult=0.5)
    feed(engine, LONG_SETUP)
    assert engine.state == IN_TRADE
    assert engine.position.entry_price == 101.5


def test_min_fvg_rejects_small_gap_in_wide_market():
    # Wide, choppy first candle (ranges ~4 pts) lifts ATR to ~3.9, so the
    # 0.5-pt FVG [100.5, 101.0] is far below 0.5 x ATR and must be voided.
    wide_fc = [
        bar("09:30", 100.0, 102.0, 98.0, 101.0),
        bar("09:31", 101.0, 102.0, 98.0, 99.0),
        bar("09:32", 99.0, 102.0, 98.0, 101.0),
        bar("09:33", 101.0, 102.0, 98.0, 99.0),
        bar("09:34", 99.0, 102.0, 98.0, 100.0),
    ]
    setup = wide_fc + [
        bar("09:35", 100.0, 100.5, 99.5, 100.2),   # b0: high 100.5
        bar("09:36", 100.2, 103.0, 100.2, 102.8),  # displacement, closes > 102
        bar("09:37", 102.8, 103.2, 101.0, 103.0),  # FVG [100.5, 101.0], h=0.5
        bar("09:38", 101.3, 101.4, 100.8, 101.1),  # retest into the zone
        bar("09:39", 101.0, 101.6, 100.9, 101.5),  # bullish engulfing
    ]
    control = make_engine()
    feed(control, setup)
    assert control.state == IN_TRADE  # baseline takes this trade

    gated = make_engine(min_fvg_atr_mult=0.5)
    feed(gated, setup)
    assert gated.position is None
    assert gated.trades == []


# ------------------------------------------------- V2: first-candle filter
def test_fc_filter_skips_wide_first_candle_day():
    # FIRST_CANDLE range is 101.0 - 99.0 = 2.0. A 1.5 limit voids the day.
    engine = make_engine(fc_range_limit=1.5)
    feed(engine, LONG_SETUP)
    assert engine.state == DONE
    assert engine.trades == []
    assert engine.position is None


def test_fc_filter_allows_normal_day():
    engine = make_engine(fc_range_limit=3.0)
    feed(engine, LONG_SETUP)
    assert engine.state == IN_TRADE


# ------------------------------------------------------------ V4: trend
def test_trend_filter_blocks_counter_body_short():
    # First-candle body is UP (100.0 -> 100.3): shorts are not allowed.
    engine = make_engine(trend_filter=True)
    feed(engine, SHORT_SETUP)
    assert engine.position is None
    assert engine.trades == []

    control = make_engine()
    feed(control, SHORT_SETUP)
    assert control.state == IN_TRADE  # same setup trades without the filter


def test_trend_filter_allows_with_body_long():
    engine = make_engine(trend_filter=True)
    feed(engine, LONG_SETUP)
    assert engine.state == IN_TRADE
    assert engine.position.direction == "long"


def test_trend_filter_blocks_long_on_down_body():
    # Same levels (high 101.0 / low 99.0) but the candle body points DOWN
    # (first open 100.0 -> last close 99.7): longs are not allowed.
    down_fc = FIRST_CANDLE[:-1] + [bar("09:34", 100.4, 100.9, 99.6, 99.7)]
    engine = make_engine(trend_filter=True)
    feed(engine, down_fc + LONG_SETUP[len(FIRST_CANDLE):])
    assert engine.position is None
    assert engine.trades == []


# ---------------------------------------------------------- V3: partials
def test_partials_bank_half_then_breakeven_stop():
    # LONG_SETUP: entry 101.5, stop 100.5, target 104.5, 5 contracts.
    # +1.5R level = 103.0. Partial: floor(5/2) = 2 units @ 103.0 ->
    # (103.0 - 101.5) x 2 x $2 = +$6.00 banked; stop moves to 101.5.
    engine = make_engine(partials=True)
    feed(engine, LONG_SETUP)
    engine.on_bar(bar("09:41", 101.6, 103.2, 101.5, 103.1))  # reaches +1.5R
    assert engine.state == IN_TRADE  # remainder still running
    engine.on_bar(bar("09:42", 103.1, 103.3, 101.4, 101.6))  # dips to BE stop
    assert engine.state == DONE
    trade = engine.trades[0]
    assert trade.partial_units == 2
    assert trade.partial_exit_time == "09:41"
    assert trade.partial_exit_price == pytest.approx(103.0)
    assert trade.partial_pnl_usd == pytest.approx(6.0)
    assert trade.exit_reason == "STOP_BE"
    assert trade.exit_price == pytest.approx(101.5)
    # Remainder leg: 3 units x (101.5 - 101.5) x $2 = $0. Total +$6.00,
    # R measured on the initial $10 risk (1.0 pt x 5 x $2) = +0.60R.
    assert trade.pnl_usd == pytest.approx(6.0)
    assert trade.r_multiple == pytest.approx(0.6)


def test_partials_remainder_reaches_target():
    engine = make_engine(partials=True)
    feed(engine, LONG_SETUP)
    engine.on_bar(bar("09:41", 101.6, 103.2, 101.5, 103.1))  # partial +$6.00
    engine.on_bar(bar("09:42", 103.1, 104.6, 103.0, 104.4))  # target 104.5
    trade = engine.trades[0]
    assert trade.exit_reason == "TARGET"
    # $6.00 banked + 3 units x 3.0 pts x $2 = $18.00 -> $24.00 = +2.40R.
    assert trade.pnl_usd == pytest.approx(24.0)
    assert trade.r_multiple == pytest.approx(2.4)


def test_partials_single_unit_just_moves_stop():
    # A $3 risk budget buys exactly 1 contract at 1.0 pt x $2/pt risk.
    engine = make_engine(partials=True, risk_budget_usd=3.0)
    feed(engine, LONG_SETUP)
    assert engine.position.units == 1
    engine.on_bar(bar("09:41", 101.6, 103.2, 101.5, 103.1))  # +1.5R: BE move
    assert engine.state == IN_TRADE
    # Low 100.9 is ABOVE the original stop (100.5) but through breakeven
    # (101.5): the trade must exit at 101.5 for $0, not run on.
    engine.on_bar(bar("09:42", 103.0, 103.1, 100.9, 101.8))
    trade = engine.trades[0]
    assert trade.partial_units == 0
    assert trade.exit_reason == "STOP_BE"
    assert trade.exit_price == pytest.approx(101.5)
    assert trade.pnl_usd == pytest.approx(0.0)


def test_partials_stop_before_trigger_is_full_loss():
    # A bar hitting the original stop before +1.5R is a plain -1R, even
    # under the partials variant.
    engine = make_engine(partials=True)
    feed(engine, LONG_SETUP)
    engine.on_bar(bar("09:41", 101.4, 101.6, 100.4, 100.6))  # stop 100.5 hit
    trade = engine.trades[0]
    assert trade.exit_reason == "STOP"
    assert trade.partial_units == 0
    assert trade.r_multiple == pytest.approx(-1.0)
