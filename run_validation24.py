#!/usr/bin/env python3
"""24-month validation of the Strategy Lab winners (paper only).

Question: do the lab's top cells survive a 24-month window
(2024-10-21 -> 2026-10-20 ET), judged net of friction?

Cells (the lab's top 5 by net-after-friction $, all with >= 15 trades
except none — every one had >= 26):
  ib_break   SPY   (+$1,115.58 after friction in the lab window)
  fcr_v0     SPY   (+$1,001.62)
  vwap_trend SPY   (+$829.15)
  orb15      SPY   (+$455.07)
  pd_sweep   MGC   (+$726.20)

Data: data/spy_1m_24mo.csv (DBEQ.BASIC, ends 2026-10-19) and
data/gc_1m_24mo.csv (GLBX.MDP3 GC.v.0 tape, traded as MGC).

STEP 1 (slice verification) runs each cell on the NEW files restricted
to the lab window 2026-06-22 -> 2026-10-20 and requires an EXACT match
with results/strategy_lab.json (trades, gross net $, net-after-
friction $). Any mismatch aborts the whole validation — no 24-month
number is trusted from an unverified route.

STEP 2 runs the full 24 months. STEP 3 applies the frozen pass test
per cell: (a) 24-month net-after-friction > 0, (b) second-half
(2025-10-21 -> end) net-after-friction > 0, (c) after-friction
realized-equity trough > $0. STEP 4 rebuilds the lab's 3-component
SPY portfolio (ib_break + fcr_v0 + vwap_trend) over the full window
with build_portfolio.py's mechanics ($500 shared account, $300
open-risk cap, exits before entries at equal timestamps, realized
equity curve); portfolio pass = net > 0 AND trough > 0.

Outputs: results/trades_<cell>_24mo.csv, results/<cell>_24mo.json,
results/trades_portfolio24_combined.csv, results/portfolio24.json,
results/validation24.json.

Run:  .venv/bin/python run_validation24.py
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from fcr.data import load_csv
from run_fcr_lab import run_v0, slice_window
from run_strategies_intraday import SYMBOL_META, run_one

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
ET = ZoneInfo("America/New_York")
STARTING_BALANCE = 500.0
RISK_BUDGET = 150.0

FULL_START = date(2024, 10, 21)
SLICE_START, SLICE_END = date(2026, 6, 22), date(2026, 10, 20)
H1_END = date(2025, 10, 20)  # H1: 2024-10-21..2025-10-20; H2: 2025-10-21..

SPY_CSV, SPY_NOTE = "spy_1m_24mo.csv", "SPY (DBEQ.BASIC)"
GC_CSV, GC_NOTE = "gc_1m_24mo.csv", "GC.v.0 tape (GLBX.MDP3)"

# strategy, symbol key, route, csv, data note, lab run id
CELLS = [
    ("ib_break", "spy", "intraday", SPY_CSV, SPY_NOTE, "ib_break_spy_120d"),
    ("fcr_v0", "spy", "fcr", SPY_CSV, SPY_NOTE, "fcr_v0_spy_120d"),
    ("vwap_trend", "spy", "intraday", SPY_CSV, SPY_NOTE,
     "vwap_trend_spy_120d"),
    ("orb15", "spy", "intraday", SPY_CSV, SPY_NOTE, "orb15_spy_120d"),
    ("pd_sweep", "mgc", "intraday", GC_CSV, GC_NOTE, "pd_sweep_mgc_120d"),
]
SPY_FRICTION = 0.04  # $/share round trip (mirrors run_fcr_lab SYMBOLS)


def money(x: float) -> float:
    return round(float(x), 2)


def intraday_run(strategy, sym_key, df, csv_name, note, start, end,
                 run_id, out_dir):
    return run_one(strategy, sym_key, df, csv_name, note, start, end,
                   STARTING_BALANCE, RISK_BUDGET, run_id, out_dir)


def fcr_run(df, start, end):
    """FCR V0 on SPY over [start, end]; returns (rows, stats, net_after)."""
    window = slice_window(df, start, end)
    rows, days, stats = run_v0(window, "SPY", 1.0, None, "equity")
    total_friction = money(sum(r["contracts"] for r in rows) * SPY_FRICTION)
    net_after = money(stats["net_usd"] - total_friction)
    return rows, days, stats, total_friction, net_after


def slice_check(cell, dfs, lab_by_id, scratch: Path) -> dict:
    strategy, sym_key, route, csv_name, note, lab_id = cell
    ref = lab_by_id[lab_id]
    df = dfs[csv_name]
    if route == "intraday":
        rec = intraday_run(strategy, sym_key, df, csv_name, note,
                           SLICE_START, SLICE_END,
                           f"val24slice_{strategy}_{sym_key}", scratch)
        got = {"trades": rec["trades"], "net_usd": rec["net_usd"],
               "net_after_friction": rec["net_usd_after_friction"]}
    else:
        _, _, stats, _, net_after = fcr_run(df, SLICE_START, SLICE_END)
        got = {"trades": stats["trades"], "net_usd": stats["net_usd"],
               "net_after_friction": net_after}
    expected = {"trades": ref["trades"], "net_usd": ref["net_usd"],
                "net_after_friction": ref["net_after_friction_usd"]}
    match = (got["trades"] == expected["trades"]
             and money(got["net_usd"]) == money(expected["net_usd"])
             and money(got["net_after_friction"])
             == money(expected["net_after_friction"]))
    print(f"SLICE {strategy:11s} {sym_key}: expected "
          f"{expected['trades']}t ${expected['net_usd']:+.2f} / "
          f"${expected['net_after_friction']:+.2f} AF | got "
          f"{got['trades']}t ${got['net_usd']:+.2f} / "
          f"${got['net_after_friction']:+.2f} AF -> "
          f"{'MATCH' if match else 'MISMATCH'}")
    return {"cell": f"{strategy}_{sym_key}", "lab_run_id": lab_id,
            "window": {"start": SLICE_START.isoformat(),
                       "end": SLICE_END.isoformat()},
            "expected": expected, "got": got, "match": match}


def enrich_from_csv(csv_path: Path, friction_per_unit: float) -> dict:
    """Half splits + after-friction trough from a written trade CSV."""
    with csv_path.open() as fh:
        rows = list(csv.DictReader(fh))
    halves = {"h1": {"trades": 0, "net_after_friction_usd": 0.0,
                     "net_r": 0.0},
              "h2": {"trades": 0, "net_after_friction_usd": 0.0,
                     "net_r": 0.0}}
    equity = STARTING_BALANCE
    trough, trough_at = STARTING_BALANCE, "start"
    ordered = sorted(rows, key=lambda r: (r["date"], r["exit_time"]))
    for r in ordered:
        if "friction_usd" in r and r["friction_usd"] not in ("", None):
            friction = float(r["friction_usd"])
        else:
            friction = float(r["contracts"]) * friction_per_unit
        net = float(r["pnl_usd"]) - friction
        d = date.fromisoformat(r["date"])
        h = halves["h1"] if d <= H1_END else halves["h2"]
        h["trades"] += 1
        h["net_after_friction_usd"] += net
        h["net_r"] += float(r["r_multiple"])
        equity += net
        if equity < trough:
            trough, trough_at = equity, f"{r['date']} {r['exit_time']}"
    for h in halves.values():
        h["net_after_friction_usd"] = money(h["net_after_friction_usd"])
        h["net_r"] = round(h["net_r"], 3)
    return {"halves": halves,
            "trough_after_friction_usd": money(trough),
            "trough_after_friction_at": trough_at}


def pass_test(net_after: float, halves: dict, trough: float) -> dict:
    a = net_after > 0
    b = halves["h2"]["net_after_friction_usd"] > 0
    c = trough > 0
    return {"a_net_after_friction_positive": a,
            "b_h2_net_after_friction_positive": b,
            "c_trough_above_zero": c,
            "passed": bool(a and b and c)}


def main() -> int:
    lab = json.loads((RESULTS / "strategy_lab.json").read_text())
    lab_by_id = {r["id"]: r for r in lab["runs"]}
    dfs = {SPY_CSV: load_csv(ROOT / "data" / SPY_CSV),
           GC_CSV: load_csv(ROOT / "data" / GC_CSV)}
    data_windows = {}
    for name, df in dfs.items():
        d = df["timestamp"].dt.date
        data_windows[name] = {"start": d.min().isoformat(),
                              "end": d.max().isoformat(),
                              "bars": int(len(df))}
    degraded = dfs[SPY_CSV][
        dfs[SPY_CSV]["timestamp"].dt.date == date(2024, 11, 19)]
    dq_note = (f"SPY 2024-11-19 flagged degraded in Databento metadata; "
               f"retained in full ({len(degraded)} bars in file) — "
               f"nothing dropped.")

    # ---- STEP 1: slice verification --------------------------------
    scratch = Path("/tmp/val24_slices")
    scratch.mkdir(parents=True, exist_ok=True)
    slices = [slice_check(c, dfs, lab_by_id, scratch) for c in CELLS]
    if not all(s["match"] for s in slices):
        print("\nSLICE VERIFICATION FAILED — aborting; no 24-month "
              "numbers produced.", file=sys.stderr)
        return 1
    print("\nSlice verification: all 5 cells match the lab exactly.\n")

    # ---- STEP 2: full 24-month runs --------------------------------
    cells_out = []
    for strategy, sym_key, route, csv_name, note, lab_id in CELLS:
        df = dfs[csv_name]
        run_id = f"{strategy}_{sym_key}_24mo"
        if route == "intraday":
            rec = intraday_run(strategy, sym_key, df, csv_name, note,
                               FULL_START, None, run_id, RESULTS)
            cell = {
                "id": run_id, "strategy": strategy,
                "strategy_label": rec["strategy_label"],
                "symbol": rec["symbol"], "symbol_key": sym_key,
                "data_file": f"data/{csv_name}", "data_symbol": note,
                "point_value_usd": rec["point_value_usd"],
                "friction_per_unit_usd": rec["friction_per_unit_usd"],
                "window": {"start": FULL_START.isoformat(),
                           "end": data_windows[csv_name]["end"]},
                "trading_days": rec["trading_days"],
                "trades": rec["trades"], "wins": rec["wins"],
                "losses": rec["losses"], "win_rate": rec["win_rate"],
                "net_usd": rec["net_usd"], "net_r": rec["net_r"],
                "net_usd_after_friction":
                    rec["net_usd_after_friction"],
                "ending_equity_usd": rec["ending_equity_usd"],
                "ending_equity_usd_after_friction":
                    rec["ending_equity_usd_after_friction"],
                "max_drawdown_usd": rec["max_drawdown_usd"],
                "skipped_for_size": rec["skipped_for_size"],
                "trade_log": rec["trade_log"],
            }
            friction_unit = rec["friction_per_unit_usd"]
        else:
            rows, days, stats, total_friction, net_after = fcr_run(
                df, FULL_START, date(2026, 10, 20))
            from run_fcr_lab import EXTENDED_COLUMNS
            log_name = f"trades_{run_id}.csv"
            pd.DataFrame(rows, columns=EXTENDED_COLUMNS).to_csv(
                RESULTS / log_name, index=False)
            cell = {
                "id": run_id, "strategy": "fcr_v0",
                "strategy_label":
                    "First Candle Rule V0 baseline (reference)",
                "symbol": "SPY", "symbol_key": "spy",
                "data_file": f"data/{csv_name}", "data_symbol": note,
                "point_value_usd": 1.0,
                "friction_per_unit_usd": SPY_FRICTION,
                "window": {"start": FULL_START.isoformat(),
                           "end": data_windows[csv_name]["end"]},
                "trading_days": stats["trading_days"],
                "trades": stats["trades"], "wins": stats["wins"],
                "losses": stats["losses"], "win_rate": stats["win_rate"],
                "net_usd": stats["net_usd"], "net_r": stats["net_r"],
                "net_usd_after_friction": net_after,
                "ending_equity_usd": stats["ending_equity_usd"],
                "ending_equity_usd_after_friction":
                    money(STARTING_BALANCE + net_after),
                "max_drawdown_usd": stats["max_drawdown_usd"],
                "skipped_for_size": stats["skipped_for_size"],
                "trade_log": f"results/{log_name}",
            }
            (RESULTS / f"{run_id}.json").write_text(
                json.dumps(cell, indent=2))
            friction_unit = SPY_FRICTION
            print(f"[{run_id}] {days} days, {stats['trades']} trades "
                  f"({stats['wins']}W/{stats['losses']}L), net "
                  f"${stats['net_usd']:+.2f} / {stats['net_r']:+.2f}R "
                  f"(after friction ${net_after:+.2f}), maxDD "
                  f"${stats['max_drawdown_usd']}, skipped "
                  f"{stats['skipped_for_size']}")
        extra = enrich_from_csv(ROOT / cell["trade_log"], friction_unit)
        cell.update(extra)
        cell["pass"] = pass_test(cell["net_usd_after_friction"],
                                 cell["halves"],
                                 cell["trough_after_friction_usd"])
        cells_out.append(cell)

    # ---- STEP 4: portfolio (3 SPY components, 24 months) -----------
    portfolio = build_portfolio24(cells_out)

    doc = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "question": "Do the Strategy Lab's top cells survive 24 "
                    "months, net of friction?",
        "window": {"start": FULL_START.isoformat(),
                   "spy_end": data_windows[SPY_CSV]["end"],
                   "mgc_end": data_windows[GC_CSV]["end"]},
        "data_windows": data_windows,
        "data_quality_note": dq_note,
        "spec": {
            "starting_balance_usd": STARTING_BALANCE,
            "risk_budget_usd": RISK_BUDGET,
            "friction_model": "same per-unit round-trip charges as "
                              "the lab (SPY $0.04/share, MGC $4.40/"
                              "contract); gross figures model no "
                              "costs",
            "pass_test": "(a) 24m net-after-friction > 0 AND "
                         "(b) H2 (2025-10-21 -> end) net-after-"
                         "friction > 0 AND (c) after-friction "
                         "realized-equity trough > $0",
            "halves": {"h1": "2024-10-21 -> 2025-10-20",
                       "h2": "2025-10-21 -> end"},
        },
        "slice_verification": slices,
        "cells": cells_out,
        "portfolio": portfolio,
    }
    (RESULTS / "validation24.json").write_text(json.dumps(doc, indent=2))

    print("\n=== 24-MONTH VALIDATION ===")
    for c in cells_out:
        p = c["pass"]
        print(f"{c['id']:22s} {c['trades']:4d}t "
              f"WR {c['win_rate']} net ${c['net_usd']:+10.2f} "
              f"AF ${c['net_usd_after_friction']:+10.2f} "
              f"H1 ${c['halves']['h1']['net_after_friction_usd']:+9.2f} "
              f"H2 ${c['halves']['h2']['net_after_friction_usd']:+9.2f} "
              f"trough ${c['trough_after_friction_usd']:9.2f} "
              f"-> {'PASS' if p['passed'] else 'FAIL'}")
    cb = portfolio["combined"]
    print(f"{'PORTFOLIO (3x SPY)':22s} {cb['trades_taken']:4d}t "
          f"net ${cb['net_usd']:+10.2f} trough "
          f"${cb['trough_usd']:9.2f} -> "
          f"{'PASS' if portfolio['pass']['passed'] else 'FAIL'}")
    print(f"\nWrote {RESULTS / 'validation24.json'}")
    return 0


def build_portfolio24(cells_out: list[dict]) -> dict:
    """Step 4 — same mechanics as build_portfolio.py, fixed
    components (the lab's 3 SPY cells), trough tracked."""
    from build_portfolio import RISK_CAP_USD, load_component_trades

    wanted = ["ib_break_spy_24mo", "fcr_v0_spy_24mo",
              "vwap_trend_spy_24mo"]
    by_id = {c["id"]: c for c in cells_out}
    events = []
    components = []
    for run_id in wanted:
        c = by_id[run_id]
        rec = {"trade_log": c["trade_log"],
               "point_value_usd": c["point_value_usd"],
               "friction_per_unit_usd": c["friction_per_unit_usd"]}
        trades = load_component_trades(rec)
        components.append({
            "run_id": run_id, "strategy": c["strategy"],
            "symbol": c["symbol"],
            "standalone": {
                "trades": c["trades"], "net_usd": c["net_usd"],
                "net_r": c["net_r"],
                "net_after_friction_usd":
                    c["net_usd_after_friction"],
                "max_drawdown_usd": c["max_drawdown_usd"],
            },
            "trades_considered": len(trades), "trades_taken": 0,
            "trades_skipped_by_cap": 0, "taken_net_usd": 0.0,
        })
        for i, t in enumerate(trades):
            key = (run_id, i)
            events.append((t["entry_dt"], 1, run_id, i, "entry", key, t))
            events.append((t["exit_dt"], 0, run_id, i, "exit", key, t))
    events.sort(key=lambda e: (e[0], e[1], e[2], e[3]))

    open_risk: dict = {}
    taken: set = set()
    combined_rows = []
    equity = STARTING_BALANCE
    peak = STARTING_BALANCE
    max_dd = 0.0
    trough, trough_at = STARTING_BALANCE, "start"
    comp_by_id = {c["run_id"]: c for c in components}
    for dt, _, run_id, _, kind, key, t in events:
        if kind == "entry":
            if sum(open_risk.values()) + t["risk_usd"] \
                    <= RISK_CAP_USD + 1e-9:
                open_risk[key] = t["risk_usd"]
                taken.add(key)
                comp_by_id[run_id]["trades_taken"] += 1
                combined_rows.append({
                    "run_id": run_id,
                    "strategy": comp_by_id[run_id]["strategy"],
                    "symbol": comp_by_id[run_id]["symbol"],
                    "direction": t["direction"],
                    "entry_dt": t["entry_dt"], "exit_dt": t["exit_dt"],
                    "risk_usd": t["risk_usd"],
                    "net_pnl_usd": t["net_pnl_usd"],
                    "taken": True, "skip_reason": "",
                })
            else:
                comp_by_id[run_id]["trades_skipped_by_cap"] += 1
                combined_rows.append({
                    "run_id": run_id,
                    "strategy": comp_by_id[run_id]["strategy"],
                    "symbol": comp_by_id[run_id]["symbol"],
                    "direction": t["direction"],
                    "entry_dt": t["entry_dt"], "exit_dt": t["exit_dt"],
                    "risk_usd": t["risk_usd"],
                    "net_pnl_usd": t["net_pnl_usd"],
                    "taken": False, "skip_reason": "risk_cap_300",
                })
        else:
            if key in taken:
                open_risk.pop(key, None)
                equity += t["net_pnl_usd"]
                comp_by_id[run_id]["taken_net_usd"] = round(
                    comp_by_id[run_id]["taken_net_usd"]
                    + t["net_pnl_usd"], 2)
                peak = max(peak, equity)
                max_dd = max(max_dd, peak - equity)
                if equity < trough:
                    trough, trough_at = equity, dt

    combined_rows.sort(key=lambda r: (r["entry_dt"], r["run_id"]))
    taken_rows = [r for r in combined_rows if r["taken"]]
    net = money(sum(r["net_pnl_usd"] for r in taken_rows))
    wins = sum(1 for r in taken_rows if r["net_pnl_usd"] > 0)
    naive = money(sum(c["standalone"]["net_after_friction_usd"]
                      for c in components))
    doc = {
        "components": components,
        "risk_cap_usd": RISK_CAP_USD,
        "starting_balance_usd": STARTING_BALANCE,
        "combined": {
            "trades_considered": len(combined_rows),
            "trades_taken": len(taken_rows),
            "trades_skipped_by_cap":
                sum(c["trades_skipped_by_cap"] for c in components),
            "wins": wins, "losses": len(taken_rows) - wins,
            "win_rate": round(wins / len(taken_rows), 4)
            if taken_rows else None,
            "net_usd": net,
            "ending_equity_usd": money(STARTING_BALANCE + net),
            "max_drawdown_usd": money(max_dd),
            "trough_usd": money(trough),
            "trough_at": trough_at,
            "naive_uncapped_sum_usd": naive,
        },
        "pass": {"net_positive": net > 0, "trough_above_zero":
                 trough > 0, "passed": bool(net > 0 and trough > 0)},
        "simplifications": (
            "Same as build_portfolio.py: realized pnl at exit times "
            "only (no mark-to-market), each trade keeps its own "
            "strategy's recorded size, $300 summed open-risk cap."),
        "trade_log": "results/trades_portfolio24_combined.csv",
    }
    (RESULTS / "portfolio24.json").write_text(json.dumps(doc, indent=2))
    cols = ["run_id", "strategy", "symbol", "direction", "entry_dt",
            "exit_dt", "risk_usd", "net_pnl_usd", "taken",
            "skip_reason"]
    with (RESULTS / "trades_portfolio24_combined.csv").open(
            "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(combined_rows)
    cb = doc["combined"]
    print(f"Portfolio: {cb['trades_taken']} taken / "
          f"{cb['trades_skipped_by_cap']} skipped-by-cap, net "
          f"${cb['net_usd']:+.2f}, WR {cb['win_rate']}, trough "
          f"${cb['trough_usd']:.2f} at {cb['trough_at']}, maxDD "
          f"${cb['max_drawdown_usd']:.2f} (naive ${naive:+.2f})")
    return doc


if __name__ == "__main__":
    raise SystemExit(main())
