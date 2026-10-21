"""Quote-matching logic for the ORB-15 options REAL-quotes rerun.

Pure functions over one contract-day of CBBO-1m NBBO rows, kept apart
from the Databento plumbing (run_orb15_options_real.py) so the
matching rules are unit-testable without any market data:

  * ENTRY = the ask of the NBBO row at the trade's entry minute;
    if that minute's row is absent (or carries no usable ask), the
    nearest row within +/-2 minutes is used and flagged.
  * The REAL premium gate is applied to that ask: > $2.50/share
    -> 'gate' (day skipped, counted separately from untradeable).
  * If no usable ask exists within +/-2 minutes (including the
    case of no rows at all that day), the trade is 'untradeable'.
  * EXIT = the bid at the trade's exit minute (same +/-2 rule);
    if no bid exists there, the last available bid before 16:00
    is used and flagged; if there is no bid at all after entry,
    the contract is booked worthless (bid 0.00) and flagged.

A price of None means "side absent" (Databento's unset sentinel,
NaN, or a negative/huge placeholder after cleaning). An ask of
0.00 is not a real ask and is treated as absent for entry; a bid
of 0.00 IS a real (worthless) bid — a long option with no bid
cannot be sold for more than nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

SESSION_START_MIN = 9 * 60 + 30   # 09:30 ET
SESSION_END_MIN = 16 * 60         # 16:00 ET
PREMIUM_GATE = 2.50               # $/share, ORB15_OPTIONS_SPEC rule 9
MAX_DELTA_MIN = 2


def occ_raw_symbol(underlying: str, expiry: date, kind: str,
                   strike: float) -> str:
    """OCC/OSI raw symbol as Databento OPRA expects it:
    underlying padded to 6 chars, YYMMDD, C/P, strike x1000 in
    8 digits. E.g. SPY 2026-10-22 call 773 -> 'SPY   261022C00773000'."""
    cp = "C" if kind == "call" else "P"
    return (f"{underlying:<6}{expiry:%y%m%d}{cp}"
            f"{int(round(strike * 1000)):08d}")


def clean_price(x) -> float | None:
    """Normalize a raw quote price: None/NaN/inf/huge-sentinel/
    negative -> None (side absent); otherwise the float value."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v) or v < 0 or v >= 1e15:
        return None
    return v


@dataclass(frozen=True)
class QuoteRow:
    minute: int            # ET minute of day (e.g. 587 = 09:47)
    bid: float | None
    ask: float | None


def _ordered_candidates(rows: list[QuoteRow], target: int,
                        max_delta: int) -> list[tuple[int, QuoteRow]]:
    """Rows within +/-max_delta of target, ordered for matching:
    smallest |delta| first, ties toward the EARLIER minute."""
    cands = []
    for r in rows:
        delta = r.minute - target
        if abs(delta) <= max_delta:
            cands.append((delta, r))
    cands.sort(key=lambda dr: (abs(dr[0]), dr[0]))
    return cands


def entry_quote(rows: list[QuoteRow], entry_minute: int,
                gate: float = PREMIUM_GATE,
                max_delta: int = MAX_DELTA_MIN) -> dict:
    """Resolve the entry ask for one trade.

    Returns {status, ask, minute, delta, flags} where status is
    'ok' (trade it), 'gate' (real ask over the premium gate) or
    'untradeable' (no usable ask within the window)."""
    if not rows:
        return {"status": "untradeable", "ask": None, "minute": None,
                "delta": None, "flags": ["no_quote_rows_that_day"]}
    for delta, row in _ordered_candidates(rows, entry_minute, max_delta):
        ask = row.ask
        if ask is None or ask <= 0:
            continue  # one-sided/absent ask on this row; try neighbor
        flags = []
        if delta != 0:
            flags.append("entry_minute_fallback")
        if ask > gate:
            return {"status": "gate", "ask": ask, "minute": row.minute,
                    "delta": delta, "flags": flags + ["premium_gate"]}
        return {"status": "ok", "ask": ask, "minute": row.minute,
                "delta": delta, "flags": flags}
    return {"status": "untradeable", "ask": None, "minute": None,
            "delta": None, "flags": ["no_ask_within_2min"]}


def exit_quote(rows: list[QuoteRow], exit_minute: int,
               entry_minute: int,
               max_delta: int = MAX_DELTA_MIN) -> dict:
    """Resolve the exit bid for one trade.

    Returns {bid, minute, delta, flags}. Falls back to the last
    available bid before 16:00 (at/after the entry minute); if no
    bid exists at all, books 0.00 ('no_exit_bid_worthless')."""
    for delta, row in _ordered_candidates(rows, exit_minute, max_delta):
        if row.bid is None:
            continue
        flags = []
        if delta != 0:
            flags.append("exit_minute_fallback")
        if row.bid == 0:
            flags.append("zero_bid")
        return {"bid": row.bid, "minute": row.minute, "delta": delta,
                "flags": flags}
    prior = [r for r in rows
             if entry_minute <= r.minute < SESSION_END_MIN
             and r.bid is not None]
    if prior:
        last = max(prior, key=lambda r: r.minute)
        flags = ["exit_last_bid_fallback"]
        if last.bid == 0:
            flags.append("zero_bid")
        return {"bid": last.bid, "minute": last.minute, "delta": None,
                "flags": flags}
    return {"bid": 0.0, "minute": None, "delta": None,
            "flags": ["no_exit_bid_worthless"]}
