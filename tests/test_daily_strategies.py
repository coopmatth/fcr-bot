"""Tests for the daily-bar strategies (fcr/daily.py).

Indicator math is checked against hand-computed values; the strategy
simulators are checked with scripted daily bars whose expected trades
(entry/exit prices, reasons, sizing) are worked out in the comments.

Run from the project root:  python3 -m pytest tests/ -q
"""
import numpy as np
import pandas as pd
import pytest

from fcr.daily import (daily_stats, prior_channel, run_donchian, run_rsi2,
                       sma, wilder_atr, wilder_rsi)


def make_bars(ohlc):
    """ohlc: list of (open, high, low, close) -> daily bars DataFrame."""
    n = len(ohlc)
    return pd.DataFrame({
        "date": pd.bdate_range("2026-01-05", periods=n),
        "open": [x[0] for x in ohlc], "high": [x[1] for x in ohlc],
        "low": [x[2] for x in ohlc], "close": [x[3] for x in ohlc],
        "volume": [0] * n,
    })


FLAT = (100.0, 101.0, 99.0, 100.0)  # TR = 2.0 every day -> ATR(14) = 2.0


# ----------------------------------------------------------------------
# Indicators
# ----------------------------------------------------------------------

def test_sma_known_series():
    out = sma(np.arange(1.0, 11.0), 5)
    assert np.isnan(out[3])
    assert out[4] == pytest.approx(3.0)   # mean(1..5)
    assert out[9] == pytest.approx(8.0)   # mean(6..10)


def test_rsi2_hand_computed():
    # Closes 10, 11, 10.5, 11.5, 11 -> changes +1, -0.5, +1, -0.5.
    # Wilder RSI(2): seed avg_gain 0.5 / avg_loss 0.25 -> RSI 66.667;
    # next: 0.75 / 0.125 -> 85.714; next: 0.375 / 0.3125 -> 54.545.
    rsi = wilder_rsi(np.array([10.0, 11.0, 10.5, 11.5, 11.0]), period=2)
    assert rsi[2] == pytest.approx(66.6667, abs=1e-3)
    assert rsi[3] == pytest.approx(85.7143, abs=1e-3)
    assert rsi[4] == pytest.approx(54.5455, abs=1e-3)


def test_rsi_all_gains_is_100():
    rsi = wilder_rsi(np.linspace(10.0, 30.0, 25), period=2)
    assert rsi[10] == pytest.approx(100.0)


def test_atr_constant_true_range():
    bars = [(100 + 0.5 * i, 101 + 0.5 * i, 99 + 0.5 * i, 100 + 0.5 * i)
            for i in range(30)]  # TR = 2.0 every bar
    atr = wilder_atr(np.array([b[1] for b in bars]),
                     np.array([b[2] for b in bars]),
                     np.array([b[3] for b in bars]), period=14)
    assert atr[14] == pytest.approx(2.0)
    assert atr[29] == pytest.approx(2.0)


def test_prior_channel_excludes_current_bar():
    ph, pl = prior_channel([1, 2, 3, 4, 500], [9, 8, 7, 6, -500], 2)
    assert ph[2] == 2.0    # max of bars 0..1, not the bar-2 value 3
    assert ph[4] == 4.0    # max of bars 2..3, not the bar-4 spike 500
    assert pl[4] == 6.0    # min of bars 2..3, not the bar-4 plunge -500


# ----------------------------------------------------------------------
# Donchian trend
# ----------------------------------------------------------------------

def donchian(bars, **kw):
    kw.setdefault("point_value", 1.0)
    kw.setdefault("max_units", None)
    kw.setdefault("friction_per_unit", 0.04)
    kw.setdefault("window_start_idx", 0)
    return run_donchian(bars, "SPY", **kw)


def test_donchian_long_breakout_channel_exit_and_sizing():
    bars = make_bars(
        [FLAT] * 25
        + [(100.0, 102.0, 100.0, 102.0)]   # 25: close 102 > prior-20 high 101
        + [(102.0, 103.0, 99.0, 102.5)]    # 26: enter at open 102
        + [(100.0, 100.5, 98.2, 98.5)]     # 27: close < prior-10 low 99
        + [(98.4, 99.0, 98.0, 98.8)])      # 28: exit at open 98.4
    rows, meta = donchian(bars)
    assert len(rows) == 1  # the day-26 close above the channel is ignored
    (r,) = rows          # while holding: one position at a time
    assert r["direction"] == "long"
    assert r["entry_price"] == 102.0
    assert r["units"] == 37                    # floor(150 / (2 x ATR 2.0))
    assert r["risk_usd"] == pytest.approx(148.0)
    assert r["exit_price"] == 98.4
    assert r["exit_reason"] == "channel_exit"
    assert r["hold_days"] == 2
    assert r["pnl_usd"] == pytest.approx(-133.2)   # (98.4 - 102) x 37
    assert r["r_multiple"] == pytest.approx(-0.9)


def test_donchian_stop_intraday_exits_at_stop_price():
    bars = make_bars(
        [FLAT] * 25
        + [(100.0, 102.0, 100.0, 102.0)]
        + [(102.0, 103.0, 99.0, 102.5)]    # enter 102, stop = 102 - 4 = 98
        + [(100.0, 100.5, 97.5, 99.0)])    # low 97.5 breaches the stop
    rows, _ = donchian(bars)
    (r,) = rows
    assert r["exit_reason"] == "stop"
    assert r["exit_price"] == 98.0
    assert r["pnl_usd"] == pytest.approx(-148.0)
    assert r["r_multiple"] == pytest.approx(-1.0)


def test_donchian_gap_through_stop_exits_at_open():
    bars = make_bars(
        [FLAT] * 25
        + [(100.0, 102.0, 100.0, 102.0)]
        + [(102.0, 103.0, 99.0, 102.5)]
        + [(97.0, 97.5, 96.0, 96.5)])      # opens at 97, below the 98 stop
    rows, _ = donchian(bars)
    (r,) = rows
    assert r["exit_reason"] == "stop_gap"
    assert r["exit_price"] == 97.0
    assert r["r_multiple"] == pytest.approx(-185.0 / 148.0)


def test_donchian_short_breakout_channel_exit():
    bars = make_bars(
        [FLAT] * 25
        + [(100.0, 100.0, 98.0, 98.0)]     # 25: close 98 < prior-20 low 99
        + [(98.0, 99.0, 97.0, 98.5)]       # 26: short at 98, stop 102
        + [(98.5, 101.8, 98.0, 101.5)]     # 27: close > prior-10 high 101
        + [(101.6, 102.0, 101.0, 101.8)])  # 28: exit at open 101.6
    rows, _ = donchian(bars)
    (r,) = rows
    assert r["direction"] == "short"
    assert r["entry_price"] == 98.0
    assert r["exit_reason"] == "channel_exit"
    assert r["exit_price"] == 101.6
    assert r["pnl_usd"] == pytest.approx(-133.2)


def test_donchian_futures_units_capped_at_five():
    tiny = (100.0, 100.1, 99.9, 100.0)     # TR = 0.2 -> ATR 0.2
    bars = make_bars(
        [tiny] * 25
        + [(100.0, 100.5, 100.0, 100.5)]   # close > prior-20 high 100.1
        + [(100.5, 100.7, 100.4, 100.6)])  # enter at 100.5, data ends
    rows, _ = donchian(bars, point_value=10.0, max_units=5,
                       friction_per_unit=4.40)
    (r,) = rows
    # Signal-day TR is 0.5 (the breakout bar), so ATR(14) at the signal
    # day = (13 x 0.2 + 0.5) / 14 = 0.2214; raw size
    # floor(150 / (2 x 0.2214 x $10)) = 33 -> capped at 5 contracts.
    assert r["units"] == 5
    assert r["risk_usd"] == pytest.approx(5 * 2 * (3.1 / 14) * 10.0,
                                          abs=0.01)
    assert r["exit_reason"] == "end_of_data"
    assert r["exit_price"] == 100.6


def test_donchian_zero_size_signal_is_skipped():
    wide = (100.0, 150.0, 50.0, 100.0)     # TR ~100 -> 2xATR > $150 budget
    bars = make_bars([wide] * 25
                     + [(100.0, 152.0, 50.0, 151.0)]   # close > high 150
                     + [(151.0, 152.0, 149.0, 150.0)])
    rows, meta = donchian(bars)
    assert rows == []
    assert meta["skipped_for_size"] == 1


def test_donchian_no_breakout_no_trades():
    rows, _ = donchian(make_bars([FLAT] * 40))
    assert rows == []


# ----------------------------------------------------------------------
# Connors RSI(2) reversion
# ----------------------------------------------------------------------

def rsi2_scenario():
    """210 steady up days (+0.4), two -3 dips (RSI(2) ~ 4.26 < 5 on the
    second), then a recovery. Expected: buy AT the second dip's close
    (177.6, index 211 — canonical Connors fills at the signal close),
    exit at the close two days later (180.6) once the close first
    exceeds SMA(5); 16 shares = floor(3000 / 177.6)."""
    closes = [100 + 0.4 * i for i in range(210)] + [180.6, 177.6, 179.6,
                                                    180.6, 181.6]
    ohlc = []
    for i, cl in enumerate(closes):
        op = closes[i - 1] if i else 100.0
        ohlc.append((op, max(op, cl) + 0.1, min(op, cl) - 0.1, cl))
    return make_bars(ohlc), closes


def test_rsi2_entry_exit_and_sizing():
    bars, closes = rsi2_scenario()
    rows, _ = run_rsi2(bars, "SPY", friction_per_unit=0.04,
                       window_start_idx=0)
    (r,) = rows
    assert r["direction"] == "long"
    assert r["entry_price"] == pytest.approx(177.6)   # signal-day close
    assert r["entry_date"] == bars["date"].iloc[211].strftime("%Y-%m-%d")
    assert r["units"] == 16                           # floor(3000 / 177.6)
    assert r["risk_usd"] == 150.0                     # the flat R proxy
    assert r["exit_reason"] == "sma5_exit"
    assert r["exit_price"] == pytest.approx(180.6)    # first close > SMA(5)
    assert r["hold_days"] == 2
    assert r["pnl_usd"] == pytest.approx(48.0)        # (180.6-177.6) x 16
    assert r["r_multiple"] == pytest.approx(0.32)
    assert r["pnl_net_usd"] == pytest.approx(48.0 - 16 * 0.04)


def test_rsi2_entry_at_signal_close_captures_overnight_gap():
    # Same scenario, but the day AFTER the signal gaps up +7.4 at the
    # open. Canonical close-entry means the position is already on at
    # 177.6 and captures the gap; next-open entry would have paid 185.
    bars, _ = rsi2_scenario()
    bars.loc[212, "open"] = 185.0
    bars.loc[212, "high"] = 185.1
    rows, _ = run_rsi2(bars, "SPY", friction_per_unit=0.04,
                       window_start_idx=0)
    (r,) = rows
    assert r["entry_price"] == pytest.approx(177.6)   # NOT the 185.0 open
    assert r["entry_date"] == bars["date"].iloc[211].strftime("%Y-%m-%d")
    assert r["exit_price"] == pytest.approx(180.6)
    assert r["pnl_usd"] == pytest.approx(48.0)


def test_rsi2_entry_day_can_be_exit_day():
    # Degenerate SMA(2)/SMA(2) periods isolate the mechanics: a long
    # -2 slide leaves RSI(2) at 0; the final +0.1 uptick keeps RSI(2)
    # at ~4.76 (< 5) while the close (48.1) clears both SMA(2)s, so the
    # signal day is itself the first close above the exit SMA — entry
    # and exit both fill at that close (48.1), hold 0 days.
    closes = [60.0, 58.0, 56.0, 54.0, 52.0, 50.0, 48.0, 48.1, 48.2]
    ohlc = []
    for i, cl in enumerate(closes):
        op = closes[i - 1] if i else 60.0
        ohlc.append((op, max(op, cl) + 0.1, min(op, cl) - 0.1, cl))
    bars = make_bars(ohlc)
    rows, _ = run_rsi2(bars, "SPY", friction_per_unit=0.04,
                       window_start_idx=0, trend_period=2, exit_period=2)
    (r,) = rows
    assert r["entry_date"] == r["exit_date"]
    assert r["entry_price"] == pytest.approx(48.1)
    assert r["exit_price"] == pytest.approx(48.1)
    assert r["exit_reason"] == "sma5_exit"
    assert r["hold_days"] == 0
    assert r["units"] == 62                            # floor(3000 / 48.1)
    assert r["pnl_usd"] == pytest.approx(0.0)
    assert r["pnl_net_usd"] == pytest.approx(-62 * 0.04)


def test_rsi2_no_signal_below_200sma():
    closes = [300 - 0.4 * i for i in range(230)]      # steady downtrend
    ohlc = []
    for i, cl in enumerate(closes):
        op = closes[i - 1] if i else 300.0
        ohlc.append((op, max(op, cl) + 0.1, min(op, cl) - 0.1, cl))
    rows, _ = run_rsi2(make_bars(ohlc), "SPY", friction_per_unit=0.04,
                       window_start_idx=0)
    assert rows == []   # RSI(2) is low but close never clears SMA(200)


def test_rsi2_window_start_blocks_early_signals():
    bars, _ = rsi2_scenario()
    rows, _ = run_rsi2(bars, "SPY", friction_per_unit=0.04,
                       window_start_idx=213)   # signal day is index 211
    assert rows == []


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------

def _row(pnl, pnl_net, r, r_net, hold=1, units=1):
    return {"pnl_usd": pnl, "pnl_net_usd": pnl_net, "r_multiple": r,
            "r_net": r_net, "hold_days": hold, "units": units}


def test_daily_stats_equity_drawdown_and_negative_flag():
    rows = [_row(100.0, 98.0, 0.5, 0.49, hold=3),
            _row(-250.0, -252.0, -1.25, -1.26, hold=5)]
    s = daily_stats(rows)
    assert s["trades"] == 2 and s["wins"] == 1
    assert s["ending_equity_usd"] == 350.0     # 500 + 100 - 250
    assert s["max_drawdown_usd"] == 250.0      # peak 600 -> 350
    assert s["equity_went_negative"] is False
    assert s["avg_hold_days"] == 4.0
    assert s["net_of_friction"]["net_usd"] == -154.0

    busted = daily_stats(rows + [_row(-400.0, -402.0, -2.0, -2.01)])
    assert busted["equity_went_negative"] is True
    assert busted["min_equity_usd"] == -50.0
