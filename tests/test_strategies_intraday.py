"""Tests for the intraday strategy pack (fcr/strategies_intraday.py).

Each strategy's core trigger is exercised with crafted bars, mirroring
the style of tests/test_variants.py. Default instrument is MGC
($10/pt, tick 0.10, cap 5) via SymbolMeta; session date 2026-10-20.

Run from the project root:  python3 -m pytest tests/ -q
"""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fcr.patterns import Bar
from fcr.strategies_intraday import STRATEGY_FACTORIES, SessionVWAP, SymbolMeta

ET = ZoneInfo("America/New_York")
DAY = "2026-10-20"
MGC = SymbolMeta(
    symbol="MGC", point_value_usd=10.0, tick_size=0.10,
    max_units=5, friction_per_unit_usd=4.40,
)
SIL = SymbolMeta(
    symbol="SIL", point_value_usd=1000.0, tick_size=0.005,
    max_units=5, friction_per_unit_usd=12.40,
)


def _dt(hhmm: str) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    return datetime(2026, 10, 20, hh, mm, tzinfo=ET)


def bar(hhmm: str, o: float, h: float, l: float, c: float, v: float = 0.0) -> Bar:
    return Bar(ts=_dt(hhmm), open=o, high=h, low=l, close=c, volume=v)


def flat(start: str, prices, v: float = 100.0) -> list[Bar]:
    """Consecutive 1-min bars with H=L=C=price, starting at `start`."""
    base = _dt(start)
    return [
        Bar(ts=base + timedelta(minutes=i), open=p, high=p, low=p, close=p, volume=v)
        for i, p in enumerate(prices)
    ]


def flat_range(start: str, end: str, price: float, v: float = 100.0) -> list[Bar]:
    """Flat bars at `price` for every minute from start to end inclusive."""
    n = int((_dt(end) - _dt(start)).total_seconds() // 60) + 1
    return flat(start, [price] * n, v=v)


def make(key: str, meta: SymbolMeta = MGC, prev_high=None, prev_low=None):
    strat = STRATEGY_FACTORIES[key](meta)
    strat.start_day(DAY, prev_high=prev_high, prev_low=prev_low)
    return strat


def feed(strat, bars) -> None:
    for b in bars:
        strat.on_bar(b)


def orb_morning() -> list[Bar]:
    """09:30-09:44 with range high 101 (09:32) and low 99 (09:38)."""
    bars = []
    for i in range(15):
        hh = 9 + (30 + i) // 60
        mm = (30 + i) % 60
        o, h, l, c = 100.0, 100.4, 99.6, 100.0
        if i == 2:
            h = 101.0
        if i == 8:
            l = 99.0
        bars.append(bar(f"{hh:02d}:{mm:02d}", o, h, l, c))
    return bars


# ------------------------------------------------------------- VWAP math
def test_session_vwap_band_math():
    vw = SessionVWAP()
    vwap, sigma = vw.update(bar("09:30", 100, 100, 100, 100, v=10))
    assert vwap == pytest.approx(100.0)
    assert sigma == pytest.approx(0.0)
    vwap, sigma = vw.update(bar("09:31", 104, 104, 104, 104, v=10))
    assert vwap == pytest.approx(102.0)
    assert sigma == pytest.approx(2.0)  # TPs 100/104 sit +/-2 around 102
    vwap, sigma = vw.update(bar("09:32", 102, 102, 102, 102, v=20))
    # E[TP^2] = (10*100^2 + 10*104^2 + 20*102^2)/40 = 10406; var = 10406-102^2 = 2
    assert vwap == pytest.approx(102.0)
    assert sigma == pytest.approx(math.sqrt(2.0))


# ------------------------------------------------------------------- ORB
def test_orb15_wick_only_does_not_trigger():
    strat = make("orb15")
    feed(strat, orb_morning())
    # High pokes above the range but the CLOSE is back inside: no entry.
    strat.on_bar(bar("09:45", 100.6, 101.8, 100.2, 100.9))
    assert strat.position is None
    feed(strat, flat_range("09:46", "10:30", 100.2))
    assert strat.position is None
    assert strat.trades == []


def test_orb15_close_break_entry_and_target_exit():
    strat = make("orb15")
    feed(strat, orb_morning())
    strat.on_bar(bar("09:45", 100.6, 101.8, 100.2, 100.9))  # wick only
    assert strat.position is None
    strat.on_bar(bar("09:46", 101.2, 101.9, 101.1, 101.6))  # close beyond
    pos = strat.position
    assert pos is not None and pos.direction == "long"
    assert pos.entry_price == pytest.approx(101.6)
    assert pos.stop == pytest.approx(99.0)            # opposite edge
    assert pos.target == pytest.approx(106.8)         # entry + 2R (R = 2.6)
    assert pos.units == 5                             # floor(150/26) capped
    strat.on_bar(bar("09:47", 102.0, 107.0, 101.5, 106.0))
    assert strat.position is None
    trade = strat.trades[0]
    assert trade.exit_reason == "TARGET"
    assert trade.exit_price == pytest.approx(106.8)
    assert trade.pnl_usd == pytest.approx(260.0)
    assert trade.r_multiple == pytest.approx(2.0)


def test_orb30_uses_30_minute_range():
    strat = make("orb30")
    feed(strat, orb_morning())  # 15-min range would be [99, 101]
    rest = flat_range("09:45", "09:59", 100.5)
    rest[1] = bar("09:46", 100.8, 101.7, 100.6, 101.6)   # beyond 15m range
    rest[5] = bar("09:50", 100.5, 105.0, 100.2, 100.5)   # 30m range high 105
    feed(strat, rest)
    assert strat.position is None  # 09:46 close must NOT trigger orb30
    strat.on_bar(bar("10:05", 103.0, 104.2, 102.8, 104.0))  # inside 30m range
    assert strat.position is None
    strat.on_bar(bar("10:06", 104.5, 105.6, 104.4, 105.4))  # close beyond 105
    pos = strat.position
    assert pos is not None and pos.direction == "long"
    assert pos.entry_price == pytest.approx(105.4)
    assert pos.stop == pytest.approx(99.0)
    assert pos.target == pytest.approx(118.2)
    assert pos.units == 2  # floor(150 / (6.4 * 10))


def test_orb15_close_runs_past_2r_to_flatten():
    strat = make("orb15_close")
    feed(strat, orb_morning())
    strat.on_bar(bar("09:45", 100.6, 101.8, 100.2, 100.9))
    strat.on_bar(bar("09:46", 101.2, 101.9, 101.1, 101.6))
    pos = strat.position
    assert pos is not None and pos.direction == "long"
    assert math.isnan(pos.target)  # no profit target by design
    # Runs to +4R (112.0) through the would-be 2R level (106.8): still open.
    strat.on_bar(bar("09:47", 105.0, 112.5, 104.0, 111.0))
    assert strat.position is not None
    assert strat.trades == []
    feed(strat, flat_range("09:48", "15:54", 108.0))
    strat.on_bar(bar("15:55", 108.3, 108.4, 108.2, 108.35))
    assert strat.position is None
    trade = strat.trades[0]
    assert trade.exit_reason == "EOD_FLATTEN"
    assert trade.exit_price == pytest.approx(108.3)
    assert trade.exit_time == "15:55"
    assert trade.r_multiple > 2.0  # banked more than the 2R version would


# --------------------------------------------------------------- pd_sweep
def test_pd_sweep_short_after_close_back_inside():
    strat = make("pd_sweep", prev_high=110.0, prev_low=90.0)
    feed(strat, [
        bar("09:30", 105.0, 105.4, 104.6, 105.0),
        bar("10:14", 108.0, 108.4, 107.6, 108.0),
    ])
    # Sweep above the prior high, but the close stays ABOVE it: no fade yet.
    strat.on_bar(bar("10:15", 110.2, 111.0, 109.0, 110.5))
    assert strat.position is None
    # Next bar closes back below the prior high: fade it. The stop uses
    # the excursion extreme (111.0 from the first bar) + 1 tick.
    strat.on_bar(bar("10:16", 110.1, 110.3, 109.2, 109.8))
    pos = strat.position
    assert pos is not None and pos.direction == "short"
    assert pos.entry_price == pytest.approx(109.8)
    assert pos.stop == pytest.approx(111.1)
    assert pos.target == pytest.approx(107.2)
    assert pos.units == 5


def test_pd_sweep_same_bar_trigger():
    strat = make("pd_sweep", prev_high=110.0, prev_low=90.0)
    strat.on_bar(bar("10:15", 110.3, 111.0, 109.4, 109.7))
    pos = strat.position
    assert pos is not None and pos.direction == "short"
    assert pos.entry_price == pytest.approx(109.7)
    assert pos.stop == pytest.approx(111.1)


def test_pd_sweep_long_mirror():
    strat = make("pd_sweep", prev_high=110.0, prev_low=90.0)
    strat.on_bar(bar("10:15", 89.6, 90.4, 89.0, 90.6))
    pos = strat.position
    assert pos is not None and pos.direction == "long"
    assert pos.entry_price == pytest.approx(90.6)
    assert pos.stop == pytest.approx(88.9)   # extreme low 89.0 - 1 tick
    assert pos.target == pytest.approx(94.0)


# ------------------------------------------------------------- VWAP revert
def test_vwap_revert_short_trigger():
    strat = make("vwap_revert")
    # Morning churn around 100: VWAP = 100, sigma = 0.2 -> bands [99.6, 100.4].
    feed(strat, flat("09:30", [99.8 if i % 2 == 0 else 100.2 for i in range(60)], v=1000.0))
    # 10:30 spike closes beyond the upper band (~100.53): arms the fade.
    strat.on_bar(bar("10:30", 100.9, 101.2, 100.85, 101.0, v=1000.0))
    assert strat.position is None  # arming is not an entry
    # 10:31 closes back inside the bands: short at that close.
    strat.on_bar(bar("10:31", 100.35, 100.4, 100.1, 100.3, v=1000.0))
    pos = strat.position
    assert pos is not None and pos.direction == "short"
    assert pos.entry_price == pytest.approx(100.3)
    assert pos.stop == pytest.approx(101.3)        # excursion high 101.2 + 1 tick
    assert pos.target == pytest.approx(100.02, abs=0.05)  # VWAP at entry
    assert pos.units == 5


# -------------------------------------------------------------- VWAP trend
def test_vwap_trend_long_pullback_and_confirm():
    strat = make("vwap_trend")
    # 30 flat bars at 100, then 30 at 101: at 10:30 close (101) > VWAP
    # (100.5) and VWAP is rising vs 10:00 (100) -> trend-up declared.
    feed(strat, flat("09:30", [100.0] * 30 + [101.0] * 30, v=100.0))
    assert strat.position is None
    # Glide down toward VWAP; only the 10:34 bar's low (100.55) lands
    # within +/-0.05% of the running VWAP (~100.516). Touch != entry.
    feed(strat, flat("10:30", [100.9, 100.8, 100.7, 100.6, 100.55], v=100.0))
    assert strat.position is None
    # Confirmation bar closes above the previous bar's high (100.55).
    feed(strat, flat("10:35", [100.6], v=100.0))
    pos = strat.position
    assert pos is not None and pos.direction == "long"
    assert pos.entry_price == pytest.approx(100.6)
    # Stop = VWAP at entry (100.5174) - 0.5 * ATR (0.0321) ~ 100.5014.
    assert pos.stop == pytest.approx(100.5014, abs=0.01)
    assert pos.target == pytest.approx(100.7973, abs=0.01)
    assert pos.units == 5


def test_vwap_trend_no_declaration_on_flat_morning():
    strat = make("vwap_trend")
    feed(strat, flat("09:30", [100.0] * 60, v=100.0))  # VWAP flat all morning
    feed(strat, flat("10:30", [99.0, 98.0, 99.5, 100.5], v=100.0))
    assert strat.position is None
    assert strat.trades == []


# ---------------------------------------------------------------- IB break
def test_ib_break_range_gate_and_midpoint_stop():
    strat = make("ib_break")
    # Morning: range-bound at 105 (H 105.5 / L 104.5), one spike high 110
    # at 09:40 and one spike low 100 at 09:50 -> IB [100, 110], mid 105.
    morning = []
    base = datetime(2026, 10, 20, 9, 30, tzinfo=ET)
    for i in range(60):
        o, h, l, c = 105.0, 105.5, 104.5, 105.0
        if i == 10:
            h = 110.0
        if i == 20:
            l = 100.0
        morning.append(Bar(ts=base + timedelta(minutes=i), open=o, high=h, low=l, close=c))
    feed(strat, morning)
    # 10:30 closes beyond the IB high but its range (0.3) is under
    # 0.5 x ATR (ATR = 1.0 from the last 14 morning bars): no entry.
    strat.on_bar(bar("10:30", 110.2, 110.6, 110.3, 110.5))
    assert strat.position is None
    # 10:31 has the range (1.1 >= 0.5 x ATR ~1.33): enter at the close.
    strat.on_bar(bar("10:31", 111.0, 112.0, 110.9, 111.8))
    pos = strat.position
    assert pos is not None and pos.direction == "long"
    assert pos.entry_price == pytest.approx(111.8)
    assert pos.stop == pytest.approx(105.0)    # IB midpoint
    assert pos.target == pytest.approx(125.4)  # entry + 2R (R = 6.8)
    assert pos.units == 2                      # floor(150 / 68)


# ------------------------------------------------- shared exit / sizing rules
def test_flatten_at_1555_bar_open():
    strat = make("orb15")
    feed(strat, orb_morning())
    strat.on_bar(bar("09:46", 101.2, 101.9, 101.1, 101.6))  # long, target 106.8
    assert strat.position is not None
    feed(strat, flat_range("09:47", "15:54", 102.0))  # never hits stop/target
    strat.on_bar(bar("15:55", 102.1, 102.2, 102.05, 102.15))
    assert strat.position is None
    trade = strat.trades[0]
    assert trade.exit_reason == "EOD_FLATTEN"
    assert trade.exit_price == pytest.approx(102.1)  # the 15:55 bar's OPEN
    assert trade.exit_time == "15:55"
    assert trade.pnl_usd == pytest.approx(25.0)


def test_skipped_for_size_counts_and_blocks_the_day():
    strat = make("orb15", meta=SIL)
    feed(strat, orb_morning())
    # Break close 101.6 vs stop 99.0 = 2.6 pts x $1000/pt = $2600/unit:
    # far beyond the $150 budget -> skip, counted, and the day is done.
    strat.on_bar(bar("09:45", 101.2, 101.9, 101.1, 101.6))
    assert strat.position is None
    assert strat.trades == []
    assert strat.skipped_for_size == 1


# --------------------------------------- execution-lag stress harness
def _lagged_orb15():
    """orb15 with the 1-bar execution-lag stress flag set."""
    strat = make("orb15")
    strat.entry_delay_bars = 1
    feed(strat, orb_morning())  # range [99, 101]
    return strat


def test_lag_default_is_zero_frozen_spec():
    assert make("orb15").entry_delay_bars == 0


def test_lag1_fills_at_next_bar_open_with_signal_levels():
    strat = _lagged_orb15()
    strat.on_bar(bar("09:45", 100.6, 101.8, 100.2, 100.9))  # wick only
    strat.on_bar(bar("09:46", 101.2, 101.9, 101.1, 101.6))  # signal close
    assert strat.position is None  # held, not filled
    strat.on_bar(bar("09:47", 101.8, 102.2, 101.5, 102.0))  # fill bar
    pos = strat.position
    assert pos is not None and pos.direction == "long"
    assert pos.entry_price == pytest.approx(101.8)   # next bar's OPEN
    assert pos.entry_time == "09:47"
    assert pos.stop == pytest.approx(99.0)           # signal-time levels
    assert pos.target == pytest.approx(106.8)
    assert pos.units == 5                            # floor(150/(2.8*10))
    strat.on_bar(bar("09:48", 103.0, 107.0, 102.5, 106.0))
    trade = strat.trades[0]
    assert trade.exit_reason == "TARGET"
    assert trade.exit_price == pytest.approx(106.8)
    assert trade.pnl_usd == pytest.approx(250.0)     # (106.8-101.8)*5*10
    assert trade.r_multiple == pytest.approx(250.0 / 140.0)


def test_lag1_fill_bar_is_live_for_stop():
    strat = _lagged_orb15()
    strat.on_bar(bar("09:46", 101.2, 101.9, 101.1, 101.6))  # signal
    # Fill bar opens inside the levels, then dips through the stop:
    # the position stops out on the fill bar itself.
    strat.on_bar(bar("09:47", 101.8, 102.0, 98.9, 99.5))
    assert strat.position is None
    trade = strat.trades[0]
    assert trade.exit_reason == "STOP"
    assert trade.exit_price == pytest.approx(99.0)
    assert trade.exit_time == "09:47"
    assert trade.pnl_usd == pytest.approx(-140.0)    # (99-101.8)*5*10
    assert trade.r_multiple == pytest.approx(-1.0)


def test_lag1_open_past_stop_voids_the_trade():
    strat = _lagged_orb15()
    strat.on_bar(bar("09:46", 101.2, 101.9, 101.1, 101.6))  # signal
    strat.on_bar(bar("09:47", 98.5, 99.2, 98.0, 98.8))      # open < stop
    assert strat.position is None
    assert strat.trades == []
    assert strat.lag_voided == 1
    feed(strat, flat_range("09:48", "10:30", 100.0))        # day is over
    assert strat.trades == []


def test_lag1_open_past_target_voids_the_trade():
    strat = _lagged_orb15()
    strat.on_bar(bar("09:46", 101.2, 101.9, 101.1, 101.6))  # signal
    strat.on_bar(bar("09:47", 107.5, 108.0, 107.0, 107.6))  # open > target
    assert strat.position is None
    assert strat.trades == []
    assert strat.lag_voided == 1
