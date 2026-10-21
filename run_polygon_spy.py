#!/usr/bin/env python3
"""Polygon (SIP) re-check of the SPY 24-month validation (paper only).

Why this exists: the 24-month validation (results/validation24.json)
found Databento's DBEQ.BASIC SPY 1-minute feed non-reproducible
across pulls — every SPY verdict is single-sample by construction.
The volume evidence below shows why: DBEQ.BASIC sums to ~2M
shares/day on SPY, a sampled fraction of the real tape. This harness
re-runs the four SPY cells on Polygon.io's consolidated (SIP)
aggregates (data/spy_1m_polygon.csv, pulled via the custom.polygon
connector, adjusted=false) under the IDENTICAL frozen spec, friction
($0.04/share) and pass test, then issues a three-sample verdict.

Steps:
  1. Tape verification — (a) daily-volume consolidation check on
     three sample days vs the Databento file, (b) determinism: an
     independent re-pull of 2026-06-15 -> 2026-06-19 must be
     bar-for-bar identical to the main pull, (c) cross-sample bar
     comparison on shared RTH timestamps (Polygon vs Databento).
  2. Full 24-month re-runs (2024-10-21 -> 2026-10-19, the Databento
     file's trading days) for ib_break, vwap_trend, orb15 (intraday
     route) and fcr_v0 (FCR route), enriched with half splits and
     the after-friction trough; frozen pass test applied.
  3. The 3-component SPY portfolio (ib_break + fcr_v0 + vwap_trend)
     rebuilt on the Polygon file with build_portfolio.py mechanics
     ($500 shared, $300 open-risk cap); pass = net > 0 AND
     trough > 0.
  4. results/polygon_verdict.json — per cell, Databento-24mo
     after-friction $ + verdict next to the Polygon numbers; a cell
     is CONFIRMED only if it PASSES on Polygon AND was positive
     after friction on the Databento 24mo file.

Run:  .venv/bin/python run_polygon_spy.py
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from fcr.data import load_csv
from run_fcr_lab import EXTENDED_COLUMNS
from run_validation24 import (
    FULL_START,
    RESULTS,
    ROOT,
    SPY_FRICTION,
    enrich_from_csv,
    fcr_run,
    intraday_run,
    money,
    pass_test,
)

ET = ZoneInfo("America/New_York")
STARTING_BALANCE = 500.0
RUN_END = date(2026, 10, 19)  # Databento 24mo file's last trading day

POLY_CSV, POLY_NOTE = "spy_1m_polygon.csv", "SPY (Polygon SIP)"
POLY_JSONL = ROOT / "data" / "spy_1m_polygon.jsonl"
REPULL_JSONL = Path("/tmp/spy_poly_week_repull.jsonl")
DB_CSV = "spy_1m_24mo.csv"

SAMPLE_DAYS = [date(2025, 3, 10), date(2026, 6, 15), date(2026, 10, 15)]
CELLS = [
    ("ib_break", "intraday"),
    ("fcr_v0", "fcr"),
    ("vwap_trend", "intraday"),
    ("orb15", "intraday"),
]


# ---------------------------------------------------------------------------
# STEP 1 — tape verification
# ---------------------------------------------------------------------------
def _day_volume(df: pd.DataFrame, day: date) -> dict:
    sub = df[df["timestamp"].dt.date == day]
    t = sub["timestamp"].dt.time
    rth = sub[(t >= time(9, 30)) & (t < time(16, 0))]
    return {"bars": int(len(sub)), "rth_bars": int(len(rth)),
            "day_volume": float(sub["volume"].sum()),
            "rth_volume": float(rth["volume"].sum())}


def tape_verification(df_poly: pd.DataFrame,
                      df_db: pd.DataFrame) -> dict:
    # (a) consolidation check --------------------------------------
    days = []
    for day in SAMPLE_DAYS:
        p, d = _day_volume(df_poly, day), _day_volume(df_db, day)
        days.append({
            "date": day.isoformat(),
            "polygon": p, "databento": d,
            "polygon_to_databento_day_volume":
                round(p["day_volume"] / d["day_volume"], 2)
                if d["day_volume"] else None,
        })
    consolidated = all(
        r["polygon"]["day_volume"] >= 20_000_000
        and r["polygon"]["day_volume"] >= 5 * r["databento"]["day_volume"]
        for r in days)

    # (b) determinism spot-check -----------------------------------
    def load_jsonl(path: Path) -> dict:
        bars = {}
        with path.open() as fh:
            for line in fh:
                b = json.loads(line)
                bars[b["t"]] = (b["o"], b["h"], b["l"], b["c"], b["v"])
        return bars

    determinism = {"repull_file": str(REPULL_JSONL),
                   "performed": REPULL_JSONL.exists()}
    if REPULL_JSONL.exists():
        main_bars = load_jsonl(POLY_JSONL)
        re_bars = load_jsonl(REPULL_JSONL)
        lo, hi = min(re_bars), max(re_bars)
        main_week = {t: v for t, v in main_bars.items() if lo <= t <= hi}
        diffs = sum(1 for t in re_bars
                    if re_bars[t] != main_week.get(t))
        determinism.update({
            "window": "2026-06-15 -> 2026-06-19 (4 trading days; "
                      "2026-06-19 is the Juneteenth holiday)",
            "repull_bars": len(re_bars),
            "main_pull_bars_in_span": len(main_week),
            "timestamp_sets_identical":
                set(re_bars) == set(main_week),
            "bars_differing_ohlcv": diffs,
            "identical": bool(set(re_bars) == set(main_week)
                              and diffs == 0),
        })

    # (c) cross-sample bar comparison (RTH, shared timestamps) -----
    def rth_close(df: pd.DataFrame) -> pd.Series:
        t = df["timestamp"].dt.time
        sub = df[(t >= time(9, 30)) & (t < time(16, 0))]
        return sub.set_index("timestamp")["close"]

    pc, dc = rth_close(df_poly), rth_close(df_db)
    shared = pc.index.intersection(dc.index)
    delta = (pc.loc[shared] - dc.loc[shared]).abs()
    differing = delta[delta > 0.001]
    cross = {
        "shared_rth_timestamps": int(len(shared)),
        "polygon_rth_bars": int(len(pc)),
        "databento_rth_bars": int(len(dc)),
        "bars_differing_close_gt_0.001": int(len(differing)),
        "share_differing": round(len(differing) / len(shared), 4)
        if len(shared) else None,
        "median_abs_dclose_differing":
            round(float(differing.median()), 4) if len(differing)
            else 0.0,
        "median_abs_dclose_all_shared": round(float(delta.median()), 4),
        "max_abs_dclose": round(float(delta.max()), 4),
        "reference": "The two Databento pulls differed on 10,234 of "
                     "37,015 overlap bars (median |dClose| $0.11) — "
                     "the disagreement this third sample arbitrates.",
    }
    return {"consolidation": {"sample_days": days,
                              "consolidated": consolidated,
                              "rule": "Polygon is the consolidated "
                                      "tape if every sample day "
                                      "shows >= 20M shares AND >= 5x "
                                      "the Databento DBEQ.BASIC day "
                                      "volume."},
            "determinism": determinism,
            "cross_sample": cross}


# ---------------------------------------------------------------------------
# STEP 3 — portfolio on the Polygon file (build_portfolio24 mechanics,
# separate output names so the Databento record is never overwritten)
# ---------------------------------------------------------------------------
def build_portfolio_poly(cells_out: list[dict]) -> dict:
    from build_portfolio import RISK_CAP_USD, load_component_trades

    wanted = ["ib_break_spy_poly", "fcr_v0_spy_poly",
              "vwap_trend_spy_poly"]
    by_id = {c["id"]: c for c in cells_out}
    events, components = [], []
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
                "max_drawdown_usd": c["max_drawdown_usd"]},
            "trades_considered": len(trades), "trades_taken": 0,
            "trades_skipped_by_cap": 0, "taken_net_usd": 0.0})
        for i, t in enumerate(trades):
            key = (run_id, i)
            events.append((t["entry_dt"], 1, run_id, i, "entry",
                           key, t))
            events.append((t["exit_dt"], 0, run_id, i, "exit",
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
    for dt, _kind_order, run_id, _i, kind, key, t in events:
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
                    "taken": True, "skip_reason": ""})
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
                    "taken": False, "skip_reason": "risk_cap_300"})
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
            "naive_uncapped_sum_usd": naive},
        "pass": {"net_positive": net > 0,
                 "trough_above_zero": trough > 0,
                 "passed": bool(net > 0 and trough > 0)},
        "simplifications": (
            "Same as build_portfolio.py: realized pnl at exit times "
            "only (no mark-to-market), each trade keeps its own "
            "strategy's recorded size, $300 summed open-risk cap."),
        "trade_log": "results/trades_portfolio_polygon_combined.csv",
    }
    (RESULTS / "portfolio_polygon.json").write_text(
        json.dumps(doc, indent=2))
    cols = ["run_id", "strategy", "symbol", "direction", "entry_dt",
            "exit_dt", "risk_usd", "net_pnl_usd", "taken",
            "skip_reason"]
    with (RESULTS / "trades_portfolio_polygon_combined.csv").open(
            "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(combined_rows)
    cb = doc["combined"]
    print(f"Portfolio (Polygon): {cb['trades_taken']} taken / "
          f"{cb['trades_skipped_by_cap']} skipped-by-cap, net "
          f"${cb['net_usd']:+.2f}, WR {cb['win_rate']}, trough "
          f"${cb['trough_usd']:.2f} at {cb['trough_at']}, maxDD "
          f"${cb['max_drawdown_usd']:.2f} (naive ${naive:+.2f})")
    return doc


# ---------------------------------------------------------------------------
def main() -> int:
    df_poly = load_csv(ROOT / "data" / POLY_CSV)
    df_db = load_csv(ROOT / "data" / DB_CSV)
    d = df_poly["timestamp"].dt.date
    in_window = df_poly[(d >= FULL_START) & (d <= RUN_END)]
    data_window = {"start": FULL_START.isoformat(),
                   "end": RUN_END.isoformat(),
                   "bars_in_window": int(len(in_window)),
                   "bars_total": int(len(df_poly))}

    # ---- STEP 1: tape verification -------------------------------
    tape = tape_verification(df_poly, df_db)
    print("=== TAPE VERIFICATION ===")
    for r in tape["consolidation"]["sample_days"]:
        print(f"  {r['date']}: Polygon day vol "
              f"{r['polygon']['day_volume']:,.0f} vs Databento "
              f"{r['databento']['day_volume']:,.0f} "
              f"({r['polygon_to_databento_day_volume']}x)")
    print(f"  consolidated: {tape['consolidation']['consolidated']}")
    det = tape["determinism"]
    print(f"  determinism: performed={det['performed']} "
          f"identical={det.get('identical')} "
          f"differing={det.get('bars_differing_ohlcv')}")
    cs = tape["cross_sample"]
    print(f"  cross-sample: {cs['bars_differing_close_gt_0.001']} of "
          f"{cs['shared_rth_timestamps']} shared RTH bars differ in "
          f"close; median |dClose| on differing "
          f"${cs['median_abs_dclose_differing']}")
    if not tape["consolidation"]["consolidated"]:
        print("\nCONSOLIDATION CHECK FAILED — Polygon Basic bars are "
              "not the consolidated tape; re-check VOID.",
              file=__import__("sys").stderr)
        return 1

    # ---- STEP 2: full 24-month re-runs on Polygon ----------------
    cells_out = []
    for strategy, route in CELLS:
        run_id = f"{strategy}_spy_poly"
        if route == "intraday":
            rec = intraday_run(strategy, "spy", df_poly, POLY_CSV,
                               POLY_NOTE, FULL_START, RUN_END,
                               run_id, RESULTS)
            cell = {
                "id": run_id, "strategy": strategy,
                "strategy_label": rec["strategy_label"],
                "symbol": "SPY", "symbol_key": "spy",
                "data_file": f"data/{POLY_CSV}",
                "data_symbol": POLY_NOTE,
                "point_value_usd": rec["point_value_usd"],
                "friction_per_unit_usd": rec["friction_per_unit_usd"],
                "window": dict(data_window),
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
            rows, days, stats, _tf, net_after = fcr_run(
                df_poly, FULL_START, RUN_END)
            log_name = f"trades_{run_id}.csv"
            pd.DataFrame(rows, columns=EXTENDED_COLUMNS).to_csv(
                RESULTS / log_name, index=False)
            cell = {
                "id": run_id, "strategy": "fcr_v0",
                "strategy_label":
                    "First Candle Rule V0 baseline (reference)",
                "symbol": "SPY", "symbol_key": "spy",
                "data_file": f"data/{POLY_CSV}",
                "data_symbol": POLY_NOTE,
                "point_value_usd": 1.0,
                "friction_per_unit_usd": SPY_FRICTION,
                "window": dict(data_window),
                "trading_days": stats["trading_days"],
                "trades": stats["trades"], "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": stats["win_rate"],
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
        cell.update(enrich_from_csv(ROOT / cell["trade_log"],
                                    friction_unit))
        cell["pass"] = pass_test(cell["net_usd_after_friction"],
                                 cell["halves"],
                                 cell["trough_after_friction_usd"])
        cell["verdict"] = "PASS" if cell["pass"]["passed"] else "FAIL"
        cells_out.append(cell)

    # ---- STEP 3: portfolio ----------------------------------------
    portfolio = build_portfolio_poly(cells_out)

    # ---- STEP 4: three-sample verdict ------------------------------
    val = json.loads((RESULTS / "validation24.json").read_text())
    db_cells = {c["id"]: c for c in val["cells"]}
    verdict_cells = []
    for cell in cells_out:
        strategy = cell["strategy"]
        db = db_cells[f"{strategy}_spy_24mo"]
        db_positive = db["net_usd_after_friction"] > 0
        confirmed = bool(cell["pass"]["passed"] and db_positive)
        if confirmed:
            call = ("CONFIRMED — passes the frozen test on the "
                    "Polygon SIP tape and was positive after "
                    "friction on the Databento 24mo file.")
        elif cell["pass"]["passed"]:
            call = ("NOT CONFIRMED — passes on Polygon, but the "
                    "Databento 24mo file had it negative after "
                    "friction; the samples disagree on the sign of "
                    "the edge.")
        else:
            failed = [k for k in ("a_net_after_friction_positive",
                                  "b_h2_net_after_friction_positive",
                                  "c_trough_above_zero")
                      if not cell["pass"][k]]
            call = ("NOT CONFIRMED — fails the frozen test on the "
                    "Polygon SIP tape (failed criteria: "
                    + ", ".join(x.split("_")[0] for x in failed)
                    + ").")
        verdict_cells.append({
            "strategy": strategy,
            "databento_24mo": {
                "data_file": db["data_file"],
                "trades": db["trades"],
                "net_usd_after_friction":
                    db["net_usd_after_friction"],
                "h1_after_friction_usd":
                    db["halves"]["h1"]["net_after_friction_usd"],
                "h2_after_friction_usd":
                    db["halves"]["h2"]["net_after_friction_usd"],
                "trough_after_friction_usd":
                    db["trough_after_friction_usd"],
                "frozen_pass": db["pass"]["passed"],
                "verdict": db["verdict"],
                "cross_sample_stability_db_pulls":
                    db.get("cross_sample_stability"),
            },
            "polygon": cell,
            "confirmed": confirmed,
            "final_call": call,
        })
    db_pf = val["portfolio"]
    pf_confirmed = bool(portfolio["pass"]["passed"]
                        and db_pf["pass"]["passed"])
    doc = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "question": "Do the Databento SPY verdicts survive a "
                    "re-check on Polygon's consolidated SIP tape?",
        "data": {
            "file": f"data/{POLY_CSV}",
            "raw": "data/spy_1m_polygon.jsonl (never published)",
            "source": "Polygon.io aggregates API via the "
                      "custom.polygon connector, adjusted=false "
                      "(raw prices), Stocks Basic plan",
            "window": data_window,
            "friction_per_unit_usd": SPY_FRICTION,
            "pass_test": "(a) 24m net-after-friction > 0 AND "
                         "(b) H2 (2025-10-21 -> end) "
                         "net-after-friction > 0 AND (c) "
                         "after-friction realized-equity trough "
                         "> $0 — identical to validation24.",
            "confirmed_rule": "CONFIRMED = passes the frozen test "
                              "on Polygon AND net-after-friction "
                              "> 0 on the Databento 24mo file.",
        },
        "tape_verification": tape,
        "cells": verdict_cells,
        "portfolio": {
            "databento_24mo": {
                "trades_taken": db_pf["combined"]["trades_taken"],
                "net_usd": db_pf["combined"]["net_usd"],
                "trough_usd": db_pf["combined"]["trough_usd"],
                "pass": db_pf["pass"]["passed"],
            },
            "polygon": portfolio,
            "confirmed": pf_confirmed,
            "final_call": (
                "CONFIRMED — the trio passes on the Polygon SIP "
                "tape as it did on the Databento file."
                if pf_confirmed else
                "NOT CONFIRMED — the trio does not pass on both "
                "tapes (see per-tape numbers)."),
        },
    }
    (RESULTS / "polygon_verdict.json").write_text(
        json.dumps(doc, indent=2))

    print("\n=== POLYGON (SIP) 24-MONTH RE-CHECK ===")
    for vc in verdict_cells:
        c = vc["polygon"]
        print(f"{c['id']:22s} {c['trades']:4d}t "
              f"WR {c['win_rate']} net ${c['net_usd']:+10.2f} "
              f"AF ${c['net_usd_after_friction']:+10.2f} "
              f"H1 ${c['halves']['h1']['net_after_friction_usd']:+9.2f} "
              f"H2 ${c['halves']['h2']['net_after_friction_usd']:+9.2f} "
              f"trough ${c['trough_after_friction_usd']:9.2f} -> "
              f"{'CONFIRMED' if vc['confirmed'] else 'NOT CONFIRMED'}")
    cb = portfolio["combined"]
    print(f"{'PORTFOLIO (3x SPY)':22s} {cb['trades_taken']:4d}t "
          f"net ${cb['net_usd']:+10.2f} trough "
          f"${cb['trough_usd']:9.2f} -> "
          f"{'CONFIRMED' if pf_confirmed else 'NOT CONFIRMED'}")
    print(f"\nWrote {RESULTS / 'polygon_verdict.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
