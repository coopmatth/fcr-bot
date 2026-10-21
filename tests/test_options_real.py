"""Unit tests for the REAL-quotes matching logic (fcr/options_real.py).

Fixtures are hand-built CBBO-1m-style rows; no market data, no API.
Rules under test (run_orb15_options_real.py / ORB15_OPTIONS_SPEC):
entry = ask at the entry minute, nearest row within +/-2 min as a
flagged fallback, no usable ask -> untradeable; real ask > $2.50 ->
premium-gate skip; exit = bid at the exit minute (same fallback),
else last bid before 16:00, else worthless 0.00.
"""
from datetime import date

from fcr.options_real import (
    QuoteRow, clean_price, entry_quote, exit_quote, occ_raw_symbol)

M = 9 * 60 + 47  # 09:47 ET, a typical entry minute


def rows(*specs):
    """specs: (minute_offset_from_M, bid, ask)"""
    return [QuoteRow(minute=M + off, bid=b, ask=a) for off, b, a in specs]


# ------------------------------------------------------------------
# symbol + price cleaning
# ------------------------------------------------------------------
def test_occ_raw_symbol_format():
    assert occ_raw_symbol("SPY", date(2026, 10, 22), "call", 773.0) \
        == "SPY   261022C00773000"
    assert occ_raw_symbol("SPY", date(2024, 10, 21), "put", 584.0) \
        == "SPY   241021P00584000"


def test_clean_price():
    assert clean_price(1.23) == 1.23
    assert clean_price(0.0) == 0.0
    assert clean_price(None) is None
    assert clean_price(float("nan")) is None
    assert clean_price(float("inf")) is None
    assert clean_price(9.223372036854776e18) is None  # unset sentinel
    assert clean_price(-0.5) is None


# ------------------------------------------------------------------
# entry matching
# ------------------------------------------------------------------
def test_entry_exact_minute():
    out = entry_quote(rows((0, 1.40, 1.43), (1, 1.50, 1.53)), M)
    assert out["status"] == "ok"
    assert out["ask"] == 1.43 and out["minute"] == M and out["delta"] == 0
    assert out["flags"] == []


def test_entry_fallback_nearest_within_2min():
    out = entry_quote(rows((-2, 1.10, 1.13), (2, 1.20, 1.23)), M)
    # tie on |delta| -> earlier minute wins
    assert out["status"] == "ok" and out["delta"] == -2
    assert out["ask"] == 1.13 and "entry_minute_fallback" in out["flags"]


def test_entry_fallback_prefers_closer_minute():
    out = entry_quote(rows((-2, 1.10, 1.13), (1, 1.20, 1.23)), M)
    assert out["delta"] == 1 and out["ask"] == 1.23


def test_entry_no_row_within_2min_is_untradeable():
    out = entry_quote(rows((-5, 1.10, 1.13), (4, 1.20, 1.23)), M)
    assert out["status"] == "untradeable"
    assert "no_ask_within_2min" in out["flags"]


def test_entry_no_rows_at_all_is_untradeable():
    out = entry_quote([], M)
    assert out["status"] == "untradeable"
    assert "no_quote_rows_that_day" in out["flags"]


def test_entry_absent_ask_row_skips_to_neighbor():
    out = entry_quote(rows((0, 1.40, None), (1, 1.50, 1.53)), M)
    assert out["status"] == "ok" and out["delta"] == 1
    assert out["ask"] == 1.53 and "entry_minute_fallback" in out["flags"]


def test_entry_zero_ask_is_not_a_real_ask():
    out = entry_quote(rows((0, 1.40, 0.0)), M)
    assert out["status"] == "untradeable"


def test_entry_premium_gate():
    over = entry_quote(rows((0, 2.55, 2.58)), M)
    assert over["status"] == "gate" and over["ask"] == 2.58
    assert "premium_gate" in over["flags"]
    at_gate = entry_quote(rows((0, 2.47, 2.50)), M)
    assert at_gate["status"] == "ok"  # gate is strictly greater-than


# ------------------------------------------------------------------
# exit matching
# ------------------------------------------------------------------
def test_exit_exact_minute():
    out = exit_quote(rows((0, 0.53, 0.56)), M, M - 43)
    assert out["bid"] == 0.53 and out["delta"] == 0 and out["flags"] == []


def test_exit_fallback_nearest():
    out = exit_quote(rows((-1, 0.61, 0.64), (2, 0.55, 0.58)), M, M - 43)
    assert out["bid"] == 0.61 and out["delta"] == -1
    assert "exit_minute_fallback" in out["flags"]


def test_exit_last_bid_before_close_fallback():
    day = [QuoteRow(minute=M - 43, bid=1.00, ask=1.03),
           QuoteRow(minute=940, bid=0.22, ask=0.25),   # 15:40
           QuoteRow(minute=955, bid=0.18, ask=0.21)]   # 15:55
    out = exit_quote(day, 955 + 30, M - 43)  # nothing near target minute
    assert out["bid"] == 0.18 and out["minute"] == 955
    assert "exit_last_bid_fallback" in out["flags"]


def test_exit_worthless_when_no_bid_anywhere():
    day = [QuoteRow(minute=M - 43, bid=None, ask=1.03),
           QuoteRow(minute=955, bid=None, ask=0.05)]
    out = exit_quote(day, 955, M - 43)
    assert out["bid"] == 0.0 and "no_exit_bid_worthless" in out["flags"]


def test_exit_zero_bid_is_real_and_flagged():
    out = exit_quote(rows((0, 0.0, 0.03)), M, M - 43)
    assert out["bid"] == 0.0 and "zero_bid" in out["flags"]
