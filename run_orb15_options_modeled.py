#!/usr/bin/env python3
"""MODELED options backtest of the ORB-15 options variant (paper only).

*** READ THIS FIRST — THIS IS A MODEL, NOT A BACKTEST ON REAL QUOTES. ***
We hold NO historical SPY options quotes. Every premium below is
ESTIMATED by pricing the frozen ORB-15 trade list's contracts with
Black-Scholes off the underlying Polygon SIP tape. The dominant
error source is the volatility input (IV): real 0DTE implied vol on a
breakout day is not the trailing realized vol, and the error
concentrates exactly where these trades live — minutes to expiry,
intraday, on days the market is moving. Treat every number here as
the middle of a BAND whose edges are the IV anchors, never as a
point estimate. The Alpaca options paper forward test (real quotes,
real fills) remains the arbiter; this model exists to decide whether
that test is worth its weeks and whether the $500 options route is
plausible at all.

Method (frozen for this run):
  * Trade list: the IDENTICAL 498 frozen ORB-15 trades of the
    Polygon verdict (regenerated with run_validation24.intraday_run
    on data/spy_1m_polygon.csv and asserted row-identical to
    results/trades_orb15_spy_poly.csv). Same entry/exit bars,
    prices, exit reasons.
  * Contract (ORB15_OPTIONS_SPEC.md): expiry = that trading day
    16:00 ET; strike = nearest $1 strike to the entry price;
    long -> CALL, short -> PUT; exactly 1 contract.
  * Pricing: Black-Scholes (fcr/options_model.py), r = 0.043 flat,
    no dividend adjustment. Entry at the signal bar's timestamp with
    spot = the frozen entry price; exit at the exit bar's timestamp
    with spot = that bar's CLOSE on the tape. Time-to-expiry floored
    at 1 minute; value floored at intrinsic.
  * IV anchors — run and reported SEPARATELY, never blended:
      (a) REALIZED       trailing 5-trading-day realized vol off the
                         tape (1-min log returns, RTH only, pooled,
                         x sqrt(252x390)), scale 1.00
      (b) REALIZED-LOW   (a) x 0.85
      (c) REALIZED-HIGH  (a) x 1.20
      (d) VIX            that day's VIX close / 100 (CBOE public
                         history CSV); a 30-day index IV applied to
                         a 0DTE contract, and the same-day close is
                         only known after the fact — two known
                         distortions, reported anyway as a market-
                         based cross-check.
  * Execution: buy at modeled ask = mid + half-spread, sell at
    modeled bid = mid - half-spread. Base half-spread $0.015/share
    ($0.03 round trip); PLUS a wide sensitivity at $0.05 round trip
    (half-spread $0.025) on anchor (a) only.
  * Premium gate (spec rule 9): modeled ask > $2.50/share -> the
    day is SKIPPED for that anchor; skipped days are counted and
    listed. P&L/trade = (exit bid - entry ask) x 100. $500 overlay,
    sequential.

Windows: the full 24 months (2024-10-21 -> 2026-10-19) and the
last 120 trading days of the tape, from the same run.

Run:  .venv/bin/python run_orb15_options_modeled.py
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
import urllib.request
from datetime import date, time
from pathlib import Path

import pandas as pd

from fcr.data import load_csv
from fcr.options_model import bs_price
from run_validation24 import FULL_START, intraday_run

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
TAPE = ROOT / "data" / "spy_1m_polygon.csv"
FROZEN_TRADES = RESULTS / "trades_orb15_spy_poly.csv"
OUT = RESULTS / "orb15_options_modeled.json"
SCRATCH = Path("/tmp/orb15_options_regen")
VIX_URL = ("https://cdn.cboe.com/api/global/us_indices/"
           "daily_prices/VIX_History.csv")
VIX_CACHE = ROOT / "data" / "vix_history.csv"

STARTING_BALANCE = 500.0
RUN_END = date(2026, 10, 19)
PREMIUM_GATE = 2.50
BASE_HALF_SPREAD = 0.015     # $0.03 round trip
WIDE_HALF_SPREAD = 0.025     # $0.05 round trip (sensitivity)
MINUTES_PER_YEAR = 252 * 390
SESSION_END_MIN = 16 * 60    # 16:00 ET expiry


def money(x: float) -> float:
    return round(x + 0.0, 2)


# ---------------------------------------------------------------------------
# STEP 0 — regenerate the frozen trade list and assert identity
# ---------------------------------------------------------------------------
def regenerate_trades() -> list[dict]:
    df = load_csv(TAPE)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    intraday_run("orb15", "spy", df, "spy_1m_polygon.csv",
                 "SPY (Polygon SIP)", FULL_START, RUN_END,
                 "orb15_spy_poly", SCRATCH)
    regen_path = SCRATCH / "trades_orb15_spy_poly.csv"
    with regen_path.open() as fh:
        regen = list(csv.DictReader(fh))
    with FROZEN_TRADES.open() as fh:
        frozen = list(csv.DictReader(fh))
    assert len(regen) == len(frozen) == 498, (len(regen), len(frozen))
    keys = ("date", "direction", "entry_time", "entry_price", "stop",
            "target", "exit_time", "exit_price", "exit_reason",
            "contracts", "pnl_usd")
    for a, b in zip(regen, frozen):
        for k in keys:
            assert a[k] == b[k], f"trade mismatch on {k}: {a} vs {b}"
    print(f"trade list regenerated: {len(regen)} trades, row-identical "
          f"to {FROZEN_TRADES.name} on all key fields")
    return frozen


# ---------------------------------------------------------------------------
# Tape structures: RTH closes by day + exit-spot lookup
# ---------------------------------------------------------------------------
def build_tape():
    df = load_csv(TAPE)
    ts = df["timestamp"]
    rth = df[(ts.dt.time >= time(9, 30)) & (ts.dt.time < time(16, 0))]
    days: dict[date, list[tuple[int, float]]] = {}
    lookup: dict[tuple[date, int], float] = {}
    for t, c in zip(rth["timestamp"], rth["close"]):
        d = t.date()
        minute = t.hour * 60 + t.minute
        days.setdefault(d, []).append((minute, float(c)))
        lookup[(d, minute)] = float(c)
    for d in days:
        days[d].sort()
    trading_days = sorted(days)
    return days, lookup, trading_days


def day_log_returns(bars: list[tuple[int, float]]) -> list[float]:
    out = []
    for (m0, c0), (m1, c1) in zip(bars, bars[1:]):
        if m1 - m0 == 1 and c0 > 0 and c1 > 0:
            out.append(math.log(c1 / c0))
    return out


def realized_vols(days, trading_days, trades) -> dict[date, dict]:
    """Anchor (a): pooled trailing-5-trading-day realized vol per
    trade date. The tape starts 2024-10-21, so the earliest trade
    dates have fewer than 5 prior days; if the pooled sample is
    still < 30 returns, supplement with that day's pre-entry RTH
    returns (flagged per date)."""
    rets_by_day = {d: day_log_returns(b) for d, b in days.items()}
    entry_min = {}
    for t in trades:
        d = date.fromisoformat(t["date"])
        hh, mm = map(int, t["entry_time"].split(":"))
        entry_min[d] = hh * 60 + mm
    out = {}
    for d in sorted(entry_min):
        idx = trading_days.index(d)
        prior = trading_days[max(0, idx - 5):idx]
        pooled: list[float] = []
        for p in prior:
            pooled.extend(rets_by_day[p])
        supplemented = False
        if len(pooled) < 30:
            pre = [r for (m0, _), (m1, _), r in
                   zip(days[d], days[d][1:], rets_by_day[d])
                   if m1 < entry_min[d]]
            pooled = pre + pooled
            supplemented = True
        if len(pooled) < 2:
            out[d] = {"sigma": None, "n_returns": len(pooled),
                      "prior_days": len(prior), "supplemented": supplemented}
            continue
        sd = statistics.stdev(pooled)
        out[d] = {"sigma": sd * math.sqrt(MINUTES_PER_YEAR),
                  "n_returns": len(pooled),
                  "prior_days": len(prior),
                  "supplemented": supplemented}
    return out


# ---------------------------------------------------------------------------
# VIX anchor
# ---------------------------------------------------------------------------
def load_vix() -> tuple[dict[date, float] | None, str]:
    try:
        if not VIX_CACHE.exists():
            req = urllib.request.Request(VIX_URL,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                VIX_CACHE.write_bytes(resp.read())
        closes = {}
        with VIX_CACHE.open() as fh:
            for row in csv.DictReader(fh):
                m, d, y = map(int, row["DATE"].split("/"))
                closes[date(y, m, d)] = float(row["CLOSE"]) / 100.0
        return closes, f"fetched/cached {VIX_CACHE.name}, " \
                         f"{len(closes)} rows through {max(closes)}"
    except Exception as exc:  # fetch/parse failure -> anchor (d) skipped
        return None, f"VIX fetch failed ({exc!r}); anchor (d) not run"


# ---------------------------------------------------------------------------
# One anchor run over a trade subset
# ---------------------------------------------------------------------------
def run_anchor(trades, lookup, sigma_of, half_spread) -> dict:
    equity = STARTING_BALANCE
    peak = equity
    trough = equity
    max_dd = 0.0
    taken, skipped = [], []
    for t in trades:
        d = date.fromisoformat(t["date"])
        sigma = sigma_of(d)
        if sigma is None:
            skipped.append({"date": t["date"], "reason": "no_sigma"})
            continue
        kind = "call" if t["direction"] == "long" else "put"
        entry_spot = float(t["entry_price"])
        strike = float(round(entry_spot))
        eh, em = map(int, t["entry_time"].split(":"))
        xh, xm = map(int, t["exit_time"].split(":"))
        entry_min = eh * 60 + em
        exit_min = xh * 60 + xm
        mid_in = bs_price(entry_spot, strike, SESSION_END_MIN - entry_min,
                          sigma, kind)
        ask_in = mid_in + half_spread
        if ask_in > PREMIUM_GATE:
            skipped.append({"date": t["date"], "reason": "premium_gate",
                            "modeled_ask": money(ask_in)})
            continue
        exit_spot = lookup.get((d, exit_min))
        if exit_spot is None:
            skipped.append({"date": t["date"], "reason": "no_exit_bar"})
            continue
        mid_out = bs_price(exit_spot, strike, SESSION_END_MIN - exit_min,
                           sigma, kind)
        bid_out = max(mid_out - half_spread, 0.0)
        pnl = (bid_out - ask_in) * 100.0
        equity += pnl
        peak = max(peak, equity)
        trough = min(trough, equity)
        max_dd = max(max_dd, peak - equity)
        taken.append({
            "date": t["date"], "direction": t["direction"], "kind": kind,
            "strike": strike, "entry_time": t["entry_time"],
            "exit_time": t["exit_time"], "exit_reason": t["exit_reason"],
            "sigma": round(sigma, 4),
            "entry_ask": money(ask_in), "exit_bid": money(bid_out),
            "options_pnl": money(pnl),
            "shares_pnl_after_friction":
                money(float(t["pnl_usd"]) - float(t["friction_usd"])),
        })
    wins = [x for x in taken if x["options_pnl"] > 0]
    losses = [x for x in taken if x["options_pnl"] <= 0]
    return {
        "trades_taken": len(taken),
        "gate_skipped": sum(1 for s in skipped
                            if s["reason"] == "premium_gate"),
        "other_skipped": [s for s in skipped
                          if s["reason"] != "premium_gate"],
        "skipped_dates": [s["date"] for s in skipped
                          if s["reason"] == "premium_gate"],
        "win_rate": round(len(wins) / len(taken), 4) if taken else None,
        "net_pnl_usd": money(sum(x["options_pnl"] for x in taken)),
        "ending_overlay_usd": money(equity),
        "trough_usd": money(trough),
        "max_drawdown_usd": money(max_dd),
        "avg_win_usd": money(statistics.mean(
            [x["options_pnl"] for x in wins])) if wins else None,
        "avg_loss_usd": money(statistics.mean(
            [x["options_pnl"] for x in losses])) if losses else None,
        "best_trade": max(taken, key=lambda x: x["options_pnl"],
                          default=None),
        "worst_trade": min(taken, key=lambda x: x["options_pnl"],
                           default=None),
        "avg_entry_ask": money(statistics.mean(
            [x["entry_ask"] for x in taken])) if taken else None,
        "trades": taken,
    }


def shares_stats(trades) -> dict:
    equity = STARTING_BALANCE
    peak = trough = equity
    max_dd = 0.0
    pnls = []
    for t in trades:
        pnl = float(t["pnl_usd"]) - float(t["friction_usd"])
        pnls.append(pnl)
        equity += pnl
        peak = max(peak, equity)
        trough = min(trough, equity)
        max_dd = max(max_dd, peak - equity)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {"trades": len(trades),
            "win_rate": round(len(wins) / len(pnls), 4),
            "net_pnl_after_friction_usd": money(sum(pnls)),
            "ending_equity_usd": money(equity),
            "trough_usd": money(trough),
            "max_drawdown_usd": money(max_dd),
            "avg_win_usd": money(statistics.mean(wins)),
            "avg_loss_usd": money(statistics.mean(losses))}


def decay_diagnostic(anchor_run: dict) -> dict:
    taken = anchor_run["trades"]
    sw_ol = [x for x in taken if x["shares_pnl_after_friction"] > 0
             and x["options_pnl"] < 0]
    sl_ow = [x for x in taken if x["shares_pnl_after_friction"] <= 0
             and x["options_pnl"] > 0]
    return {
        "basis": "trades TAKEN under anchor (a) base spread only",
        "shares_winners": sum(1 for x in taken
                              if x["shares_pnl_after_friction"] > 0),
        "shares_winners_that_lost_in_options": len(sw_ol),
        "shares_losers": sum(1 for x in taken
                             if x["shares_pnl_after_friction"] <= 0),
        "shares_losers_that_won_in_options": len(sl_ow),
        "shares_win_to_options_loss_dates": [x["date"] for x in sw_ol],
        "shares_loss_to_options_win_dates": [x["date"] for x in sl_ow],
    }


def main() -> int:
    trades = regenerate_trades()
    days, lookup, trading_days = build_tape()
    vols = realized_vols(days, trading_days, trades)
    vix, vix_note = load_vix()
    print(f"VIX anchor: {vix_note}")

    def sig_realized(d):
        v = vols[d]
        return v["sigma"]

    def sig_low(d):
        s = vols[d]["sigma"]
        return None if s is None else s * 0.85

    def sig_high(d):
        s = vols[d]["sigma"]
        return None if s is None else s * 1.20

    def sig_vix(d):
        if vix is None:
            return None
        if d in vix:
            return vix[d]
        prior = [k for k in vix if k < d]
        return vix[max(prior)] if prior else None

    anchors = [
        ("realized", "REALIZED (trailing 5d realized vol x1.00)",
         sig_realized, BASE_HALF_SPREAD),
        ("realized_low", "REALIZED-LOW (realized x0.85)",
         sig_low, BASE_HALF_SPREAD),
        ("realized_high", "REALIZED-HIGH (realized x1.20)",
         sig_high, BASE_HALF_SPREAD),
        ("realized_wide_spread",
         "REALIZED with WIDE spread ($0.05 round trip)",
         sig_realized, WIDE_HALF_SPREAD),
    ]
    if vix is not None:
        anchors.append(("vix", "VIX (same-day VIX close /100)",
                        sig_vix, BASE_HALF_SPREAD))

    in_window_days = [d for d in trading_days if FULL_START <= d <= RUN_END]
    last120_start = in_window_days[-120]
    windows = {
        "full_24mo": [t for t in trades],
        "last_120d": [t for t in trades
                      if date.fromisoformat(t["date"]) >= last120_start],
    }
    print(f"windows: full={len(windows['full_24mo'])} trades; "
          f"last 120 trading days from {last120_start}: "
          f"{len(windows['last_120d'])} trades")

    doc = {
        "generated_for": "Matt's question 2026-10-21: what would the "
                         "ORB-15 OPTIONS variant have done over 120 "
                         "days and over 2 years?",
        "honesty_banner": "THIS IS A MODEL, NOT A BACKTEST ON REAL "
                          "QUOTES. We hold no historical options "
                          "quotes; every premium is Black-Scholes "
                          "off the underlying tape with an ASSUMED "
                          "volatility. IV is the dominant error "
                          "source and the error concentrates exactly "
                          "where 0DTE trades live (minutes to "
                          "expiry, breakout-day IV spikes). Read the "
                          "band across anchors, never a single "
                          "number. The Alpaca options paper forward "
                          "test (real quotes, real fills) is the "
                          "arbiter.",
        "model": {
            "pricer": "Black-Scholes (fcr/options_model.py), stdlib "
                      "math.erf",
            "risk_free_rate": 0.043,
            "dividend_adjustment": "none (noted limitation)",
            "expiry": "trade day 16:00 ET",
            "strike": "nearest $1 strike to entry price",
            "multiplier": 100,
            "contracts": 1,
            "premium_gate_usd_per_share": PREMIUM_GATE,
            "base_half_spread_usd": BASE_HALF_SPREAD,
            "wide_half_spread_usd": WIDE_HALF_SPREAD,
            "entry_spot": "frozen trade entry price (signal bar close)",
            "exit_spot": "close of the frozen trade's exit bar",
            "trade_list": "identical 498 trades of "
                          "results/trades_orb15_spy_poly.csv "
                          "(regenerated and asserted row-identical)",
            "tape": "data/spy_1m_polygon.csv (Polygon SIP)",
            "vol_fallback_dates": [d.isoformat() for d, v in
                                   sorted(vols.items())
                                   if v["supplemented"]],
        },
        "vix_anchor": {"status": vix_note},
        "windows": {},
    }

    for wname, wtrades in windows.items():
        w = {"shares_identical_trades": shares_stats(wtrades),
             "anchors": {}, "decay_diagnostic_anchor_a": None}
        for key, label, fn, hs in anchors:
            run = run_anchor(wtrades, lookup, fn, hs)
            run["label"] = label
            run["half_spread_usd"] = hs
            w["anchors"][key] = run
            if key == "realized":
                w["decay_diagnostic_anchor_a"] = decay_diagnostic(run)
        doc["windows"][wname] = w

    # ---- sanity 1: modeled ATM entry premiums (anchor a, full) ----
    full_a = doc["windows"]["full_24mo"]["anchors"]["realized"]
    asks = sorted(x["entry_ask"] for x in full_a["trades"])
    n = len(asks)
    doc["sanity"] = {
        "entry_ask_distribution_anchor_a_full": {
            "n": n,
            "median": money(asks[n // 2]),
            "p10": money(asks[n // 10]),
            "p90": money(asks[9 * n // 10]),
            "min": money(asks[0]), "max": money(asks[-1]),
            "share_outside_1_to_3_50":
                round(sum(1 for a in asks if a < 1.0 or a > 3.50) / n, 4),
            "flag": "OK" if 1.0 <= asks[n // 2] <= 3.50 else
                    "MEDIAN OUTSIDE PLAUSIBLE $1-$3.50 BAND",
        }
    }

    # ---- sanity 2: ground truth, 2026-10-21 ~15:44 ET ---------------
    gt_days = [d for d in trading_days if d < date(2026, 10, 21)][-5:]
    pooled = []
    for d in gt_days:
        pooled.extend(day_log_returns(days[d]))
    gt_sigma = statistics.stdev(pooled) * math.sqrt(MINUTES_PER_YEAR)
    gt = {"moment": "2026-10-21 ~15:44 ET", "spot": 773.07,
          "strike": 773.0, "kind": "call", "minutes_to_expiry": 16,
          "actual_ask_usd": 0.84,
          "realized_vol_basis": f"5 trading days ending 2026-10-20 "
                                f"(tape has no 2026-10-21 bars)",
          "modeled": {}}
    gt_vix = vix[max(k for k in vix if k <= date(2026, 10, 20))] \
        if vix else None
    for label, s in [("realized", gt_sigma),
                     ("realized_low", gt_sigma * 0.85),
                     ("realized_high", gt_sigma * 1.20),
                     ("vix_2026-10-20_close", gt_vix)]:
        if s is None:
            continue
        mid = bs_price(773.07, 773.0, 16, s, "call")
        gt["modeled"][label] = {"sigma": round(s, 4),
                                "mid": money(mid),
                                "ask": money(mid + BASE_HALF_SPREAD)}
    doc["ground_truth_check"] = gt

    OUT.write_text(json.dumps(doc, indent=1))
    print(f"\nwrote {OUT}")

    # ---- printed analysis --------------------------------------------
    for wname, w in doc["windows"].items():
        sh = w["shares_identical_trades"]
        print(f"\n===== {wname} =====")
        print(f"SHARES (identical trades): {sh['trades']}t WR "
              f"{sh['win_rate']:.1%} net AF ${sh['net_pnl_after_friction_usd']:+,.2f} "
              f"end ${sh['ending_equity_usd']:,.2f} trough "
              f"${sh['trough_usd']:,.2f} maxDD ${sh['max_drawdown_usd']:,.2f}")
        for key, run in w["anchors"].items():
            print(f"  {key:22s} taken {run['trades_taken']:3d} "
                  f"gate-skip {run['gate_skipped']:3d} WR "
                  f"{run['win_rate']:.1%} net ${run['net_pnl_usd']:+10,.2f} "
                  f"end ${run['ending_overlay_usd']:9,.2f} trough "
                  f"${run['trough_usd']:8,.2f} maxDD "
                  f"${run['max_drawdown_usd']:8,.2f} avgW "
                  f"${run['avg_win_usd']} avgL ${run['avg_loss_usd']}")
        dd = w["decay_diagnostic_anchor_a"]
        print(f"  decay: {dd['shares_winners_that_lost_in_options']} of "
              f"{dd['shares_winners']} shares-winners LOST in options; "
              f"{dd['shares_losers_that_won_in_options']} of "
              f"{dd['shares_losers']} shares-losers WON in options")
    s1 = doc["sanity"]["entry_ask_distribution_anchor_a_full"]
    print(f"\nsanity 1 (entry asks, anchor a): median ${s1['median']} "
          f"p10 ${s1['p10']} p90 ${s1['p90']} range "
          f"${s1['min']}-${s1['max']} outside$1-3.50 "
          f"{s1['share_outside_1_to_3_50']:.1%} -> {s1['flag']}")
    print(f"sanity 2 (ground truth 2026-10-21 15:44, actual ask $0.84):")
    for label, m in gt["modeled"].items():
        print(f"  {label:22s} sigma {m['sigma']:.3f} modeled ask "
              f"${m['ask']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
