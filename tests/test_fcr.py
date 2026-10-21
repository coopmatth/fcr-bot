"""Unit + scenario tests for the First Candle Rule engine.

Run from the project root:  python3 -m pytest tests/ -q
"""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fcr.config import load_config
from fcr.engine import (
    DONE,
    IN_TRADE,
    WAIT_BREAK,
    FCREngine,
    compute_target,
    compute_units,
    position_size,
)
from fcr.patterns import Bar, detect_fvg, is_bearish_engulfing, is_bullish_engulfing

ET = ZoneInfo("America/New_York")
CFG = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
DAY = "2026-10-20"


def bar(hhmm: str, o: float, h: float, l: float, c: float) -> Bar:
    hh, mm = map(int, hhmm.split(":"))
    return Bar(ts=datetime(2026, 10, 20, hh, mm, tzinfo=ET), open=o, high=h, low=l, close=c)


def make_engine() -> FCREngine:
    engine = FCREngine(CFG)
    engine.start_day(DAY)
    return engine


def feed(engine: FCREngine, bars) -> None:
    for b in bars:
        engine.on_bar(b)


# First candle 9:30-9:34: high 101.0, low 99.0.
FIRST_CANDLE = [
    bar("09:30", 100.0, 100.6, 99.4, 100.2),
    bar("09:31", 100.2, 101.0, 99.8, 100.5),
    bar("09:32", 100.5, 100.8, 99.0, 100.1),
    bar("09:33", 100.1, 100.7, 99.6, 100.4),
    bar("09:34", 100.4, 100.9, 100.0, 100.3),
]

# Bullish break + FVG [100.5, 101.2], retest, bullish-engulfing entry at
# 09:40 close 101.5, stop 100.5. Risk distance 1.0 pt on MNQ ($2/pt) ->
# floor(150 / 2) = 75 contracts, capped at 5. Target 104.5.
LONG_SETUP = FIRST_CANDLE + [
    bar("09:35", 100.2, 100.5, 99.9, 100.3),    # b0 of the FVG triplet
    bar("09:36", 100.3, 101.9, 100.3, 101.7),   # displacement, closes > 101.0
    bar("09:37", 101.7, 102.0, 101.2, 101.8),   # b2: low 101.2 > b0 high 100.5
    bar("09:38", 101.8, 101.9, 101.3, 101.4),   # no retest yet (low > FVG top)
    bar("09:39", 101.4, 101.45, 100.9, 101.0),  # retest into zone, red bar
    bar("09:40", 100.95, 101.6, 100.9, 101.5),  # bullish engulfing -> ENTRY
]


# ------------------------------------------------------------ pattern units
def test_bullish_fvg_detection():
    b0 = bar("09:35", 100.0, 100.5, 99.8, 100.2)
    b1 = bar("09:36", 100.2, 101.5, 100.2, 101.3)
    b2 = bar("09:37", 101.3, 101.6, 101.0, 101.4)
    fvg = detect_fvg(b0, b1, b2)
    assert fvg is not None
    assert fvg.direction == "bullish"
    assert fvg.bottom == 100.5 and fvg.top == 101.0


def test_bearish_fvg_detection():
    b0 = bar("09:35", 100.0, 100.2, 99.5, 99.8)
    b1 = bar("09:36", 99.8, 99.9, 98.5, 98.7)
    b2 = bar("09:37", 98.7, 99.0, 98.2, 98.5)
    fvg = detect_fvg(b0, b1, b2)
    assert fvg is not None
    assert fvg.direction == "bearish"
    assert fvg.bottom == 99.0 and fvg.top == 99.5


def test_no_fvg_when_wicks_overlap():
    b0 = bar("09:35", 100.0, 100.5, 99.8, 100.2)
    b1 = bar("09:36", 100.2, 100.6, 100.0, 100.4)
    b2 = bar("09:37", 100.4, 100.7, 100.2, 100.5)
    assert detect_fvg(b0, b1, b2) is None


def test_bullish_engulfing():
    prev = bar("09:39", 101.4, 101.45, 100.9, 101.0)   # bearish
    cur = bar("09:40", 100.95, 101.6, 100.9, 101.5)    # engulfs prev body
    assert is_bullish_engulfing(prev, cur)
    weak = bar("09:40", 100.95, 101.4, 100.9, 101.3)   # close < prev open
    assert not is_bullish_engulfing(prev, weak)


def test_bearish_engulfing():
    prev = bar("09:39", 98.0, 98.9, 97.95, 98.7)       # bullish
    cur = bar("09:40", 98.75, 98.8, 97.9, 98.0)        # engulfs prev body
    assert is_bearish_engulfing(prev, cur)
    weak = bar("09:40", 98.75, 98.8, 98.1, 98.2)       # close > prev open
    assert not is_bearish_engulfing(prev, weak)


# ------------------------------------------------------------- math units
def test_position_size_math():
    assert position_size(1000, 0.50) == 2000   # $1000 risk / $0.50 distance
    assert position_size(1000, 1.50) == 666    # floored to whole shares
    assert position_size(1000, 0.0) == 0
    assert position_size(1000, 1500.0) == 0   # stop wider than the budget


def test_compute_units_mnq_contract_sizing():
    # MNQ: $2 per point per contract, $150 risk budget.
    assert compute_units(150, 15.0, 2.0) == 5    # floor(150 / 30) = 5
    assert compute_units(150, 40.0, 2.0) == 1    # floor(150 / 80) = 1
    assert compute_units(150, 100.0, 2.0) == 0   # stop too wide -> skip
    assert compute_units(150, 75.0, 2.0) == 1    # exactly affordable
    assert compute_units(150, 75.01, 2.0) == 0   # just over -> skip


def test_compute_units_cap():
    # Raw count would be 15; the 5-contract cap binds.
    assert compute_units(150, 5.0, 2.0, max_units=5) == 5
    # Under the cap, the budget rules.
    assert compute_units(150, 40.0, 2.0, max_units=5) == 1
    # Uncapped (equity-style) matches position_size.
    assert compute_units(1000, 0.50, 1.0) == 2000
    assert compute_units(1000, 0.50, 1.0, max_units=None) == 2000


def test_target_math_3r():
    assert compute_target("long", 100.0, 99.5, 3.0) == pytest.approx(101.5)
    assert compute_target("short", 100.0, 100.5, 3.0) == pytest.approx(98.5)


# --------------------------------------------------------- engine scenarios
def test_first_candle_levels():
    engine = make_engine()
    feed(engine, FIRST_CANDLE)
    assert engine.level_high is None  # candle not complete until the 09:35 bar
    engine.on_bar(bar("09:35", 100.3, 100.5, 99.9, 100.2))
    assert engine.level_high == 101.0
    assert engine.level_low == 99.0
    assert engine.state == WAIT_BREAK


def test_full_long_trade_hits_3r_target():
    engine = make_engine()
    feed(engine, LONG_SETUP)
    assert engine.state == IN_TRADE
    assert engine.position.entry_price == 101.5
    assert engine.position.stop == 100.5
    assert engine.position.target == 104.5
    assert engine.position.units == 5  # 75 contracts by budget, capped at 5
    feed(
        engine,
        [
            bar("09:41", 101.5, 102.5, 101.4, 102.4),
            bar("09:42", 102.4, 104.6, 102.3, 104.4),  # high reaches target
        ],
    )
    assert engine.state == DONE
    assert len(engine.trades) == 1
    trade = engine.trades[0]
    assert trade.direction == "long"
    assert trade.exit_reason == "TARGET"
    assert trade.exit_price == 104.5
    assert trade.pnl_usd == pytest.approx(30.0)  # 3.0 pts x 5 contracts x $2/pt
    assert trade.r_multiple == pytest.approx(3.0)


def test_full_short_trade_hits_3r_target():
    engine = make_engine()
    feed(
        engine,
        FIRST_CANDLE
        + [
            bar("09:35", 99.8, 100.1, 99.5, 99.7),    # b0: low 99.5
            bar("09:36", 99.7, 99.8, 97.9, 98.2),     # displacement, closes < 99
            bar("09:37", 98.2, 98.6, 97.9, 98.3),     # b2: high 98.6 < 99.5 -> FVG
            bar("09:38", 98.3, 98.5, 97.8, 98.0),     # no retest (high < FVG bottom)
            bar("09:39", 98.0, 98.9, 97.95, 98.7),    # retest into zone, green bar
            bar("09:40", 98.75, 98.8, 97.9, 98.0),    # bearish engulfing -> ENTRY
        ],
    )
    assert engine.state == IN_TRADE
    assert engine.position.direction == "short"
    assert engine.position.entry_price == 98.0
    assert engine.position.stop == 99.5
    assert engine.position.units == 5  # floor(150 / (1.5 x $2)) = 50, capped at 5
    assert engine.position.target == pytest.approx(93.5)
    feed(
        engine,
        [
            bar("09:41", 98.0, 98.4, 96.0, 96.2),
            bar("09:42", 96.2, 96.3, 93.4, 93.8),  # low reaches target
        ],
    )
    assert len(engine.trades) == 1
    trade = engine.trades[0]
    assert trade.exit_reason == "TARGET"
    assert trade.pnl_usd == pytest.approx(45.0)  # 4.5 pts x 5 contracts x $2/pt
    assert trade.r_multiple == pytest.approx(3.0)


def test_one_trade_per_day_lock():
    engine = make_engine()
    feed(engine, LONG_SETUP)
    # Stop the long out quickly.
    engine.on_bar(bar("09:41", 101.4, 101.5, 100.4, 100.6))
    assert engine.state == DONE
    assert len(engine.trades) == 1
    assert engine.trades[0].exit_reason == "STOP"
    assert engine.trades[0].r_multiple == pytest.approx(-1.0)
    # A complete, valid bearish setup follows (break of the low + bearish
    # FVG + retest + bearish engulfing). The lock must ignore it.
    feed(
        engine,
        [
            bar("09:42", 100.6, 100.7, 100.0, 100.1),
            bar("09:43", 100.1, 100.3, 99.9, 100.0),
            bar("09:44", 100.0, 100.05, 98.2, 98.4),   # displacement through 99.0
            bar("09:45", 98.4, 98.7, 98.1, 98.5),      # bearish FVG completes
            bar("09:46", 98.5, 99.5, 98.4, 99.2),      # retest into the zone
            bar("09:47", 99.3, 99.35, 98.3, 98.4),     # bearish engulfing
        ],
    )
    assert engine.state == DONE
    assert len(engine.trades) == 1  # still exactly one trade


def test_no_entry_after_cutoff():
    engine = make_engine()
    feed(engine, FIRST_CANDLE)
    # Flat chop from 09:35 to 11:57 — no break, no FVG.
    t = datetime(2026, 10, 20, 9, 35, tzinfo=ET)
    end = datetime(2026, 10, 20, 11, 57, tzinfo=ET)
    while t <= end:
        engine.on_bar(Bar(ts=t, open=100.3, high=100.4, low=100.2, close=100.3))
        t += timedelta(minutes=1)
    # A textbook bullish break+FVG straddling the 12:00 cutoff.
    feed(
        engine,
        [
            bar("11:58", 100.3, 100.5, 100.1, 100.3),
            bar("11:59", 100.3, 101.9, 100.3, 101.7),
            bar("12:00", 101.7, 102.0, 101.2, 101.8),  # FVG completes AT cutoff
            bar("12:01", 101.4, 101.45, 100.9, 101.0),
            bar("12:02", 100.95, 101.6, 100.9, 101.5),  # would-be engulfing entry
        ],
    )
    assert engine.trades == []
    assert engine.position is None
    assert engine.state == DONE


def test_skipped_for_size_when_stop_too_wide():
    engine = make_engine()
    feed(
        engine,
        FIRST_CANDLE
        + [
            bar("09:35", 100.2, 100.5, 99.9, 100.3),      # b0: high 100.5
            bar("09:36", 100.3, 195.0, 100.3, 194.0),     # huge displacement
            bar("09:37", 194.0, 196.0, 190.0, 195.0),     # FVG [100.5, 190]
            bar("09:38", 195.0, 195.2, 189.5, 190.0),     # retest, bearish bar
            bar("09:39", 189.8, 196.0, 189.5, 195.5),     # bullish engulfing
        ],
    )
    # Entry would be 195.5 with stop 100.5: 95 pts x $2 = $190 per contract,
    # over the $150 budget -> 0 contracts -> setup discarded and counted.
    assert engine.position is None
    assert engine.trades == []
    assert engine.skipped_for_size == 1
    assert engine.state == WAIT_BREAK


def _day_df(day: int, spec_rows):
    import pandas as pd

    records = []
    for hhmm, o, h, l, c in spec_rows:
        hh, mm = map(int, hhmm.split(":"))
        records.append(
            {
                "timestamp": datetime(2026, 10, day, hh, mm, tzinfo=ET),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(records)


def test_backtest_equity_tracking():
    from fcr.backtest import run_backtest, run_stats

    setup = [
        ("09:30", 100.0, 100.6, 99.4, 100.2),
        ("09:31", 100.2, 101.0, 99.8, 100.5),
        ("09:32", 100.5, 100.8, 99.0, 100.1),
        ("09:33", 100.1, 100.7, 99.6, 100.4),
        ("09:34", 100.4, 100.9, 100.0, 100.3),
        ("09:35", 100.2, 100.5, 99.9, 100.3),
        ("09:36", 100.3, 101.9, 100.3, 101.7),
        ("09:37", 101.7, 102.0, 101.2, 101.8),
        ("09:38", 101.8, 101.9, 101.3, 101.4),
        ("09:39", 101.4, 101.45, 100.9, 101.0),
        ("09:40", 100.95, 101.6, 100.9, 101.5),  # entry long, 5 contracts
    ]
    win_day = setup + [
        ("09:41", 101.5, 102.5, 101.4, 102.4),
        ("09:42", 102.4, 104.6, 102.3, 104.4),  # target hit: +$30
    ]
    loss_day = setup + [
        ("09:41", 101.4, 101.5, 100.4, 100.6),  # stop hit: -$10
    ]
    import pandas as pd

    df = pd.concat([_day_df(20, win_day), _day_df(21, loss_day)]).reset_index(drop=True)
    rows, days, meta = run_backtest(df, CFG)
    assert days == 2
    assert len(rows) == 2
    assert meta["starting_balance_usd"] == 500.0
    assert meta["ending_equity_usd"] == pytest.approx(520.0)
    assert meta["max_drawdown_usd"] == pytest.approx(10.0)
    assert [p["equity"] for p in meta["equity_curve"]] == [500.0, 530.0, 520.0]
    stats = run_stats(rows, days, meta)
    assert stats["trades"] == 2
    assert stats["wins"] == 1 and stats["losses"] == 1
    assert stats["win_rate"] == 0.5
    assert stats["net_usd"] == pytest.approx(20.0)
    assert stats["net_r"] == pytest.approx(2.0)
    assert stats["avg_contracts"] == 5.0
    assert stats["skipped_for_size"] == 0
