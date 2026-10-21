#!/usr/bin/env python3
"""MES cross-check of the two credible SPY winners (paper only).

Context: the 24-month validation left exactly two SPY cells worth
believing. IB breakout PASSED all three frozen criteria on the
single new SPY file and was STABLE across the two DBEQ.BASIC pulls;
VWAP trend was also STABLE (and made +$4,013.33 after friction on
the new file) but FAILED the trough criterion standalone. Both,
however, rest on Databento's DBEQ.BASIC tape, which is NOT
reproducible across pulls — so neither verdict is feed-independent.

This phase re-runs the same two strategies on MES (ES.v.0 tape,
GLBX.MDP3 — deterministic: the lab reproduces to the cent on it),
traded as MES at $5/pt with the lab's $4.90/contract round-trip
friction, over the full 24 months (2024-10-21 -> 2026-10-20):

  STEP 1  Slice verification: both cells restricted to the lab
          window (2026-06-22 -> 2026-10-20) on the NEW 24-month ES
          file must EXACTLY match results/strategy_lab.json
          (ib_break MES: 64 trades, +$728.77 / +$224.07 AF;
          vwap_trend MES: 47 trades, -$1,313.94 / -$2,210.64 AF).
          GLBX is deterministic, so a match is expected; any
          mismatch aborts the phase before a single 24-month
          number is produced.
  STEP 2  Full 24-month runs + the frozen pass test: (a) 24m
          net-after-friction > 0, (b) H2 (2025-10-21 -> end)
          net-after-friction > 0, (c) after-friction realized-
          equity trough > $0. Half splits + trough from
          enrich_from_csv, identical to the SPY validation.
  STEP 3  Mini-portfolio ONLY if BOTH cells pass: ib_break +
          vwap_trend on MES in one shared $500 account, $300
          open-risk cap, build_portfolio24 mechanics; pass =
          net > 0 AND trough > 0. If either cell fails, no
          portfolio is built and the JSON says why.

FRAMING (printed on the dashboard and in the README): MES and SPY
are the same index, but ES futures trade nearly around the clock,
so the 9:30 ET 'open' for MES is a continuation of overnight
trading rather than SPY's auction open — a strategy keyed to the
cash open can legitimately behave differently on the two, so a
MES failure weakens but does not by itself falsify the SPY result,
and a MES pass is strong feed-independent confirmation.

Outputs: results/trades_<cell>_mes_24mo.csv, results/<cell>_mes_24mo.json,
results/crosscheck_mes.json (+ results/portfolio_mes.json and
results/trades_portfolio_mes_combined.csv if the portfolio runs).

Run:  .venv/bin/python run_crosscheck_mes.py
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fcr.data import load_csv
from run_validation24 import (
    FULL_START,
    RESULTS,
    ROOT,
    SLICE_END,
    SLICE_START,
    enrich_from_csv,
    intraday_run,
    money,
    pass_test,
)

ET = ZoneInfo("America/New_York")
STARTING_BALANCE = 500.0

ES_CSV, ES_NOTE = "es_1m_24mo.csv", "ES.v.0 tape (GLBX.MDP3)"
DEGRADED_DAYS = ["2025-09-17", "2025-09-24", "2025-11-28"]
FRAMING_NOTE = (
    "MES and SPY are the same index, but ES futures trade nearly "
    "around the clock, so the 9:30 ET 'open' for MES is a "
    "continuation of overnight trading rather than SPY's auction "
    "open — a strategy keyed to the cash open can legitimately "
    "behave differently on the two, so a MES failure weakens but "
    "does not by itself falsify the SPY result, and a MES pass is "
    "strong feed-independent confirmation.")

# strategy, lab run id (in results/strategy_lab.json)
CELLS = [
    ("ib_break", "ib_break_mes_120d"),
    ("vwap_trend", "vwap_trend_mes_120d"),
]


def slice_check(strategy, lab_id, df, lab_by_id, scratch: Path) -> dict:
    ref = lab_by_id[lab_id]
    rec = intraday_run(strategy, "mes", df, ES_CSV, ES_NOTE,
                       SLICE_START, SLICE_END,
                       f"xcheck_slice_{strategy}_mes", scratch)
    got = {"trades": rec["trades"], "net_usd": rec["net_usd"],
           "net_after_friction": rec["net_usd_after_friction"]}
    expected = {"trades": ref["trades"], "net_usd": ref["net_usd"],
                "net_after_friction": ref["net_after_friction_usd"]}
    match = (got["trades"] == expected["trades"]
             and money(got["net_usd"]) == money(expected["net_usd"])
             and money(got["net_after_friction"])
             == money(expected["net_after_friction"]))
    print(f"SLICE {strategy:11s} mes: expected "
          f"{expected['trades']}t ${expected['net_usd']:+.2f} / "
          f"${expected['net_after_friction']:+.2f} AF | got "
          f"{got['trades']}t ${got['net_usd']:+.2f} / "
          f"${got['net_after_friction']:+.2f} AF -> "
          f"{'MATCH' if match else 'MISMATCH'}")
    return {"cell": f"{strategy}_mes", "lab_run_id": lab_id,
            "window": {"start": SLICE_START.isoformat(),
                       "end": SLICE_END.isoformat()},
            "expected": expected, "got": got, "match": match}


def build_portfolio_mes(cells_out: list[dict]) -> dict:
    """Two-component mini-portfolio — build_portfolio24 mechanics
    (exits before entries at equal timestamps, $300 summed
    open-risk cap, realized-P&L equity curve with trough)."""
    from build_portfolio import RISK_CAP_USD, load_component_trades

    events = []
    components = []
    for c in cells_out:
        rec = {"trade_log": c["trade_log"],
               "point_value_usd": c["point_value_usd"],
               "friction_per_unit_usd": c["friction_per_unit_usd"]}
        trades = load_component_trades(rec)
        components.append({
            "run_id": c["id"], "strategy": c["strategy"],
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
            key = (c["id"], i)
            events.append((t["entry_dt"], 1, c["id"], i, "entry",
                           key, t))
            events.append((t["exit_dt"], 0, c["id"], i, "exit",
                           key, t))
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
                    "entry_dt": t["entry_dt"],
                    "exit_dt": t["exit_dt"],
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
                    "entry_dt": t["entry_dt"],
                    "exit_dt": t["exit_dt"],
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
        "built": True,
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
        "pass": {"net_positive": net > 0,
                 "trough_above_zero": trough > 0,
                 "passed": bool(net > 0 and trough > 0)},
        "simplifications": (
            "Same as build_portfolio.py / build_portfolio24: "
            "realized pnl at exit times only (no mark-to-market), "
            "each trade keeps its own strategy's recorded size, "
            "$300 summed open-risk cap."),
        "trade_log": "results/trades_portfolio_mes_combined.csv",
    }
    (RESULTS / "portfolio_mes.json").write_text(json.dumps(doc,
                                                           indent=2))
    cols = ["run_id", "strategy", "symbol", "direction", "entry_dt",
            "exit_dt", "risk_usd", "net_pnl_usd", "taken",
            "skip_reason"]
    with (RESULTS / "trades_portfolio_mes_combined.csv").open(
            "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(combined_rows)
    cb = doc["combined"]
    print(f"MES portfolio: {cb['trades_taken']} taken / "
          f"{cb['trades_skipped_by_cap']} skipped-by-cap, net "
          f"${cb['net_usd']:+.2f}, WR {cb['win_rate']}, trough "
          f"${cb['trough_usd']:.2f} at {cb['trough_at']}, maxDD "
          f"${cb['max_drawdown_usd']:.2f} (naive ${naive:+.2f}) -> "
          f"{'PASS' if doc['pass']['passed'] else 'FAIL'}")
    return doc


def main() -> int:
    lab = json.loads((RESULTS / "strategy_lab.json").read_text())
    lab_by_id = {r["id"]: r for r in lab["runs"]}
    df = load_csv(ROOT / "data" / ES_CSV)
    d = df["timestamp"].dt.date
    data_window = {"start": d.min().isoformat(),
                   "end": d.max().isoformat(),
                   "bars": int(len(df))}
    degraded_counts = {
        day: int((d == datetime.fromisoformat(day).date()).sum())
        for day in DEGRADED_DAYS}
    dq_note = (
        "Databento flagged the whole GLBX.MDP3 dataset for degraded "
        "quality on 2025-09-17, 2025-09-24 and 2025-11-28 (the same "
        "flags noted on the gold file); the ES file retains those "
        "days in full (bars in file: "
        + ", ".join(f"{k}: {v}" for k, v in degraded_counts.items())
        + ") — nothing dropped.")

    doc: dict = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "question": "Do the two credible SPY winners (IB breakout, "
                    "VWAP trend) hold up on MES — the same index on "
                    "the deterministic GLBX.MDP3 futures tape — over "
                    "24 months, net of friction?",
        "window": {"start": FULL_START.isoformat(),
                   "end": data_window["end"]},
        "data_file": f"data/{ES_CSV}",
        "data_window": data_window,
        "data_quality_note": dq_note,
        "spec": {
            "starting_balance_usd": STARTING_BALANCE,
            "risk_budget_usd": 150,
            "traded_as": "MES, $5/pt, friction $4.90/contract "
                          "round trip (same as the lab)",
            "pass_test": "(a) 24m net-after-friction > 0 AND "
                         "(b) H2 (2025-10-21 -> end) net-after-"
                         "friction > 0 AND (c) after-friction "
                         "realized-equity trough > $0",
            "halves": {"h1": "2024-10-21 -> 2025-10-20",
                       "h2": "2025-10-21 -> end"},
        },
        "framing_note": FRAMING_NOTE,
    }

    # ---- STEP 1: slice verification --------------------------------
    scratch = Path("/tmp/xcheck_mes_slices")
    scratch.mkdir(parents=True, exist_ok=True)
    slices = [slice_check(s, lab_id, df, lab_by_id, scratch)
              for s, lab_id in CELLS]
    doc["slice_verification"] = slices
    if not all(s["match"] for s in slices):
        doc["status"] = ("ABORTED — slice verification failed on "
                         "the new ES file; no 24-month numbers "
                         "produced. See slice_verification for the "
                         "expected-vs-got record.")
        (RESULTS / "crosscheck_mes.json").write_text(
            json.dumps(doc, indent=2))
        print("\nSLICE VERIFICATION FAILED — aborting; no 24-month "
              "numbers produced.", file=sys.stderr)
        return 1
    print("\nSlice verification: both MES cells match the lab "
          "exactly on the new file.\n")

    # ---- STEP 2: full 24-month runs --------------------------------
    cells_out = []
    for strategy, _lab_id in CELLS:
        run_id = f"{strategy}_mes_24mo"
        rec = intraday_run(strategy, "mes", df, ES_CSV, ES_NOTE,
                           FULL_START, None, run_id, RESULTS)
        cell = {
            "id": run_id, "strategy": strategy,
            "strategy_label": rec["strategy_label"],
            "symbol": rec["symbol"], "symbol_key": "mes",
            "data_file": f"data/{ES_CSV}", "data_symbol": ES_NOTE,
            "point_value_usd": rec["point_value_usd"],
            "friction_per_unit_usd": rec["friction_per_unit_usd"],
            "window": {"start": FULL_START.isoformat(),
                       "end": data_window["end"]},
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
        cell.update(enrich_from_csv(ROOT / cell["trade_log"],
                                    rec["friction_per_unit_usd"]))
        cell["pass"] = pass_test(cell["net_usd_after_friction"],
                                 cell["halves"],
                                 cell["trough_after_friction_usd"])
        cell["verdict"] = "PASS" if cell["pass"]["passed"] else "FAIL"
        cells_out.append(cell)
    doc["cells"] = cells_out

    # ---- STEP 3: mini-portfolio only if BOTH pass ------------------
    if all(c["pass"]["passed"] for c in cells_out):
        doc["portfolio"] = build_portfolio_mes(cells_out)
    else:
        failed = [c["id"] for c in cells_out
                  if not c["pass"]["passed"]]
        doc["portfolio"] = {
            "built": False,
            "reason": ("Mini-portfolio not built: the frozen rule "
                       "builds it only if BOTH cells pass; failed: "
                       + ", ".join(failed) + "."),
        }
        print("Mini-portfolio skipped — not both cells passed "
              f"(failed: {', '.join(failed)}).")

    spy_ref = {}
    val = json.loads((RESULTS / "validation24.json").read_text())
    for c in val["cells"]:
        if c["id"] in ("ib_break_spy_24mo", "vwap_trend_spy_24mo"):
            spy_ref[c["strategy"]] = {
                "net_usd_after_friction":
                    c["net_usd_after_friction"],
                "verdict": c["verdict"],
                "cross_sample_stability":
                    c.get("cross_sample_stability"),
            }
    doc["spy_reference"] = spy_ref
    doc["status"] = "COMPLETE"
    (RESULTS / "crosscheck_mes.json").write_text(
        json.dumps(doc, indent=2))

    print("\n=== MES CROSS-CHECK — 24 MONTHS ===")
    for c in cells_out:
        p = c["pass"]
        print(f"{c['id']:22s} {c['trades']:4d}t "
              f"WR {c['win_rate']} net ${c['net_usd']:+10.2f} "
              f"AF ${c['net_usd_after_friction']:+10.2f} "
              f"H1 ${c['halves']['h1']['net_after_friction_usd']:+9.2f} "
              f"H2 ${c['halves']['h2']['net_after_friction_usd']:+9.2f} "
              f"trough ${c['trough_after_friction_usd']:9.2f} "
              f"-> {c['verdict']}")
    print(f"\nWrote {RESULTS / 'crosscheck_mes.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
