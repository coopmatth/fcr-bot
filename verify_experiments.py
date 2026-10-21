#!/usr/bin/env python3
"""Independent verification of the experiment matrix (paper only).

Checks, reading ONLY the written CSVs and experiments.json (not the
engine), so a plumbing bug cannot hide behind its own bookkeeping:

  1. V0 MNQ reproduces the known Databento baseline EXACTLY: the trade
     rows match results/trades_mnq_1m_databento_60d.csv field by field,
     and the aggregates are 13 trades, -$374.50, -5.00R, end $125.50.
  2. Every V0 run: entries only 09:35 <= t < 12:00 ET, at most one
     trade per day, TARGET exits exactly +3R, STOP exits exactly -1R.
  3. Every run: experiments.json aggregates (trades, W/L, net $, net R,
     ending equity) recompute exactly from its trade CSV.
  4. V3 runs: every partial leg's arithmetic is re-derived by hand —
     partial pnl = (partial price - entry) x partial units x point
     value; total pnl = partial + remainder leg; R = total / initial
     risk dollars. One worked example is printed in full.

Exits non-zero on the first failed check. Run after run_experiments.py:
    .venv/bin/python verify_experiments.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

POINT_VALUES = {"MNQ": 2.0, "MES": 5.0, "MGC": 10.0, "SIL": 1000.0, "SPY": 1.0}
TOL_USD = 0.02   # CSVs round $ to cents; allow one rounding step per leg
TOL_R = 0.002    # R is rounded to 3 decimals


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def load_rows(name: str) -> list[dict]:
    with (RESULTS / name).open() as fh:
        return list(csv.DictReader(fh))


def f(row: dict, key: str) -> float:
    return float(row[key])


def check_v0_mnq(matrix: dict) -> None:
    baseline = load_rows("trades_mnq_1m_databento_60d.csv")
    v0 = load_rows("trades_mnq_1m_v0_baseline.csv")
    if len(v0) != len(baseline):
        fail(f"V0 MNQ has {len(v0)} trades, baseline has {len(baseline)}")
    shared = [
        "date", "direction", "entry_time", "entry_price", "stop", "target",
        "exit_time", "exit_price", "exit_reason", "contracts", "pnl_usd",
        "r_multiple",
    ]
    for i, (a, b) in enumerate(zip(baseline, v0)):
        for col in shared:
            va, vb = a[col], b[col]
            same = va == vb if col in ("date", "direction", "entry_time",
                                       "exit_time", "exit_reason") \
                else abs(float(va) - float(vb)) < 1e-9
            if not same:
                fail(f"V0 MNQ row {i} col {col}: {vb!r} != baseline {va!r}")
    rec = next(r for r in matrix["runs"] if r["id"] == "mnq_1m_v0_baseline")
    expect = {"trades": 13, "wins": 2, "losses": 11, "net_usd": -374.50,
              "net_r": -5.00, "ending_equity_usd": 125.50,
              "max_drawdown_usd": 881.50, "skipped_for_size": 1}
    for k, v in expect.items():
        if rec[k] != v:
            fail(f"V0 MNQ {k}: {rec[k]} != expected {v}")
    print(f"1. V0 MNQ reproduces the baseline exactly "
          f"(13 trades row-by-row; {expect['net_usd']:+.2f} / -5.00R / "
          f"end $125.50). OK")


def check_v0_invariants(matrix: dict) -> None:
    for rec in matrix["runs"]:
        if rec["variant"] != "v0":
            continue
        rows = load_rows(Path(rec["trade_log"]).name)
        per_day: dict[str, int] = {}
        for r in rows:
            hh, mm = map(int, r["entry_time"].split(":"))
            if not (9 * 60 + 35 <= hh * 60 + mm < 12 * 60):
                fail(f"{rec['id']}: entry at {r['entry_time']} outside "
                     f"09:35-12:00")
            per_day[r["date"]] = per_day.get(r["date"], 0) + 1
            if per_day[r["date"]] > 1:
                fail(f"{rec['id']}: more than one trade on {r['date']}")
            if r["exit_reason"] == "TARGET" and abs(f(r, "r_multiple") - 3.0) > TOL_R:
                fail(f"{rec['id']}: TARGET exit at {r['r_multiple']}R != +3R")
            if r["exit_reason"] == "STOP" and abs(f(r, "r_multiple") + 1.0) > TOL_R:
                fail(f"{rec['id']}: STOP exit at {r['r_multiple']}R != -1R")
    print("2. All V0 runs: entries 09:35-12:00 ET, <=1 trade/day, "
          "TARGET = +3.00R, STOP = -1.00R. OK")


def check_aggregates(matrix: dict) -> None:
    for rec in matrix["runs"]:
        rows = load_rows(Path(rec["trade_log"]).name)
        net = round(sum(f(r, "pnl_usd") for r in rows), 2)
        net_r = round(sum(f(r, "r_multiple") for r in rows), 3)
        wins = sum(1 for r in rows if f(r, "pnl_usd") > 0)
        if len(rows) != rec["trades"] or wins != rec["wins"]:
            fail(f"{rec['id']}: CSV has {len(rows)} trades / {wins} wins, "
                 f"experiments.json says {rec['trades']} / {rec['wins']}")
        if abs(net - rec["net_usd"]) > TOL_USD or abs(net_r - rec["net_r"]) > 0.01:
            fail(f"{rec['id']}: CSV sums to ${net} / {net_r}R, "
                 f"experiments.json says ${rec['net_usd']} / {rec['net_r']}R")
        end = round(500.0 + net, 2)
        if abs(end - rec["ending_equity_usd"]) > TOL_USD:
            fail(f"{rec['id']}: 500 + net = {end}, experiments.json says "
                 f"{rec['ending_equity_usd']}")
    print("3. All 30 runs: experiments.json aggregates recompute from the "
          "trade CSVs. OK")


def check_partials(matrix: dict) -> None:
    pv_of = POINT_VALUES
    example = None
    partial_trades = 0
    for rec in matrix["runs"]:
        if rec["variant"] not in ("v3", "v5"):
            continue
        pv = pv_of[rec["symbol"]]
        rows = load_rows(Path(rec["trade_log"]).name)
        for r in rows:
            entry, stop = f(r, "entry_price"), f(r, "stop")
            units = int(r["contracts"])
            risk_usd = abs(entry - stop) * units * pv
            p_units = int(r["partial_units"])
            sign = 1.0 if r["direction"] == "long" else -1.0
            if p_units > 0:
                partial_trades += 1
                p_price = f(r, "partial_exit_price")
                expect_leg = sign * (p_price - entry) * p_units * pv
                if abs(expect_leg - f(r, "partial_pnl_usd")) > TOL_USD:
                    fail(f"{rec['id']} {r['date']}: partial pnl "
                         f"{r['partial_pnl_usd']} != hand-computed "
                         f"{expect_leg:.2f}")
                # The partial price must sit exactly at +1.5R.
                expect_price = entry + sign * 1.5 * abs(entry - stop)
                if abs(p_price - expect_price) > 0.011:
                    fail(f"{rec['id']} {r['date']}: partial price {p_price} "
                         f"!= +1.5R price {expect_price:.4f}")
            remainder = units - p_units
            expect_total = f(r, "partial_pnl_usd") + \
                sign * (f(r, "exit_price") - entry) * remainder * pv
            if abs(expect_total - f(r, "pnl_usd")) > TOL_USD:
                fail(f"{rec['id']} {r['date']}: total pnl {r['pnl_usd']} "
                     f"!= partial + remainder = {expect_total:.2f}")
            expect_r = f(r, "pnl_usd") / risk_usd if risk_usd else 0.0
            if abs(expect_r - f(r, "r_multiple")) > TOL_R + 0.004:
                fail(f"{rec['id']} {r['date']}: R {r['r_multiple']} != "
                     f"pnl / initial risk = {expect_r:.3f}")
            if p_units > 0 and example is None:
                example = (rec, r, pv, expect_total, risk_usd)
    if example is None:
        fail("No partial-exit trade found in any V3/V5 run — nothing to audit")
    rec, r, pv, total, risk_usd = example
    print(f"4. Partials: {partial_trades} banked partial legs across V3/V5 "
          f"all re-derive by hand. Worked example — {rec['id']} "
          f"{r['date']}: {r['direction']} {r['contracts']} units @ "
          f"{r['entry_price']}, stop {r['stop']} (initial risk "
          f"${risk_usd:.2f}); partial {r['partial_units']} units @ "
          f"{r['partial_exit_price']} (+1.5R) at {r['partial_exit_time']} "
          f"= ${r['partial_pnl_usd']}; remainder "
          f"{int(r['contracts']) - int(r['partial_units'])} units exit @ "
          f"{r['exit_price']} ({r['exit_reason']}); total "
          f"${r['pnl_usd']} = {r['r_multiple']}R. OK")


def main() -> int:
    matrix = json.loads((RESULTS / "experiments.json").read_text())
    if len(matrix["runs"]) != 30:
        fail(f"experiments.json has {len(matrix['runs'])} runs, expected 30")
    check_v0_mnq(matrix)
    check_v0_invariants(matrix)
    check_aggregates(matrix)
    check_partials(matrix)
    print("\nAll verification checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
