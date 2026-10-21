#!/usr/bin/env python3
"""Independent verification of the gold 24-month validation (paper only).

Reads ONLY results/gold24.json, the three trade CSVs, and
results/experiments.json — never the engine — so a plumbing bug cannot
hide behind its own bookkeeping:

  1. Every gold24 headline stat (trades, W/L, win rate, net $, net R,
     ending equity, max drawdown, equity trough + date, longest losing
     streak) recomputes from its trade CSV. Drawdown/trough rebuild
     from the day-end equity series implied by the CSV (no-trade days
     don't move equity, so trade days alone reproduce the curve's
     peaks and troughs). Half splits recompute the same way, with the
     second half seeded at the first half's ending equity.
  2. V0 invariants: entries only 09:35 <= t < 12:00 ET, at most one
     trade per day, TARGET exits exactly +3R, STOP exits exactly -1R.
  3. The pass criteria re-derive from the recomputed stats and the
     winner is the first passing variant in V0 -> V3 -> V5 order.
  4. The 60-day MGC V0 result is untouched: experiments.json still
     records +$584.00 / 18 trades, and a fresh run_backtest on
     data/gc_1m_databento.csv reproduces it exactly.

Not recomputed here (not derivable from trade CSVs, which contain no
rows for skipped setups or no-trade days): skipped_for_size and the
per-half trading-day counts. Those come from run_gold24.py, whose
tracked driver is itself cross-checked against run_backtest there.

Exits non-zero on the first failed check. Run after run_gold24.py:
    .venv/bin/python verify_gold24.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

TOL_USD = 0.011
TOL_R = 0.01
HALF_SPLIT = "2025-10-20"
START_EQUITY = 500.0


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def load_rows(name: str) -> list[dict]:
    with (RESULTS / name).open() as fh:
        return list(csv.DictReader(fh))


def f(row: dict, key: str) -> float:
    return float(row[key])


def close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def streak(rows: list[dict]) -> int:
    best = cur = 0
    for r in rows:
        if f(r, "pnl_usd") < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def curve_stats(day_pnl: list[tuple[str, float]], opening: float) -> dict:
    """Day-end equity series -> (end equity, max DD, trough, trough date)."""
    equity = opening
    peak = opening
    max_dd = 0.0
    trough, trough_date = opening, "start"
    for date, pnl in day_pnl:
        equity = round(equity + pnl, 2)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if equity < trough:
            trough, trough_date = equity, date
    return {"ending": equity, "max_dd": round(max_dd, 2),
            "trough": trough, "trough_date": trough_date}


def segment_from_rows(rows: list[dict], opening: float) -> dict:
    wins = sum(1 for r in rows if f(r, "pnl_usd") > 0)
    by_day: dict[str, float] = {}
    for r in rows:
        by_day[r["date"]] = by_day.get(r["date"], 0.0) + f(r, "pnl_usd")
    cs = curve_stats(sorted(by_day.items()), opening)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": round(wins / len(rows), 4) if rows else None,
        "net_usd": round(sum(f(r, "pnl_usd") for r in rows), 2),
        "net_r": round(sum(f(r, "r_multiple") for r in rows), 3),
        "ending_equity_usd": cs["ending"],
        "max_drawdown_usd": cs["max_dd"],
        "equity_trough_usd": cs["trough"],
        "equity_trough_date": cs["trough_date"],
        "longest_losing_streak": streak(rows),
    }


def compare(run_id: str, label: str, got: dict, want: dict) -> None:
    for k in ("trades", "wins", "losses", "longest_losing_streak"):
        if got[k] != want[k]:
            fail(f"{run_id} {label}: {k} recompute {got[k]} != gold24 {want[k]}")
    for k in ("net_usd", "ending_equity_usd", "max_drawdown_usd", "equity_trough_usd"):
        if not close(got[k], want[k], TOL_USD):
            fail(f"{run_id} {label}: {k} recompute {got[k]} != gold24 {want[k]}")
    if not close(got["net_r"], want["net_r"], TOL_R):
        fail(f"{run_id} {label}: net_r recompute {got['net_r']} != gold24 {want['net_r']}")
    if got["win_rate"] != want["win_rate"]:
        fail(f"{run_id} {label}: win_rate recompute {got['win_rate']} != gold24 {want['win_rate']}")
    if got["equity_trough_date"] != want["equity_trough_date"]:
        fail(f"{run_id} {label}: trough date recompute {got['equity_trough_date']} "
             f"!= gold24 {want['equity_trough_date']}")


def check_recompute(doc: dict) -> dict:
    recomputed = {}
    for rec in doc["runs"]:
        rows = load_rows(Path(rec["trade_log"]).name)
        total = segment_from_rows(rows, START_EQUITY)
        compare(rec["id"], "total", total, rec)
        h1_rows = [r for r in rows if r["date"] <= HALF_SPLIT]
        h2_rows = [r for r in rows if r["date"] > HALF_SPLIT]
        h1 = segment_from_rows(h1_rows, START_EQUITY)
        h2 = segment_from_rows(h2_rows, h1["ending_equity_usd"])
        compare(rec["id"], "H1", h1, rec["halves"]["first"])
        compare(rec["id"], "H2", h2, rec["halves"]["second"])
        recomputed[rec["variant"]] = (total, h2)
        print(f"   {rec['id']}: totals + both halves recompute from the CSV "
              f"(net {total['net_usd']:+.2f} / {total['net_r']:+.3f}R, "
              f"trough {total['equity_trough_usd']} on {total['equity_trough_date']}). OK")
    print("1. All gold24 headline stats + half splits recompute from the "
          "trade CSVs. OK")
    return recomputed


def check_v0_invariants(doc: dict) -> None:
    rec = next(r for r in doc["runs"] if r["variant"] == "v0")
    rows = load_rows(Path(rec["trade_log"]).name)
    per_day: dict[str, int] = {}
    for r in rows:
        hh, mm = map(int, r["entry_time"].split(":"))
        if not (9 * 60 + 35 <= hh * 60 + mm < 12 * 60):
            fail(f"V0: entry at {r['entry_time']} on {r['date']} outside 09:35-12:00")
        per_day[r["date"]] = per_day.get(r["date"], 0) + 1
        if per_day[r["date"]] > 1:
            fail(f"V0: more than one trade on {r['date']}")
        if r["exit_reason"] == "TARGET" and abs(f(r, "r_multiple") - 3.0) > 0.002:
            fail(f"V0 {r['date']}: TARGET exit at {r['r_multiple']}R != +3R")
        if r["exit_reason"] == "STOP" and abs(f(r, "r_multiple") + 1.0) > 0.002:
            fail(f"V0 {r['date']}: STOP exit at {r['r_multiple']}R != -1R")
        if int(r["partial_units"]) != 0:
            fail(f"V0 {r['date']}: baseline trade has a partial leg")
    print("2. V0 invariants: entries 09:35-12:00 ET, <=1 trade/day, "
          "TARGET = +3.00R, STOP = -1.00R, no partial legs. OK")


def check_verdict(doc: dict, recomputed: dict) -> None:
    order = ("v0", "v3", "v5")
    expect_winner = None
    for vkey in order:
        total, h2 = recomputed[vkey]
        checks = {
            "a_full_run_positive": total["net_usd"] > 0 and total["net_r"] > 0,
            "b_second_half_positive": h2["net_usd"] > 0 and h2["net_r"] > 0,
            "c_trough_above_zero": total["equity_trough_usd"] > 0,
        }
        rec = next(r for r in doc["runs"] if r["variant"] == vkey)
        if rec["pass_checks"] != checks:
            fail(f"{vkey}: gold24 pass_checks {rec['pass_checks']} != recomputed {checks}")
        if rec["passed"] != all(checks.values()):
            fail(f"{vkey}: gold24 passed={rec['passed']} != recomputed {all(checks.values())}")
        if expect_winner is None and all(checks.values()):
            expect_winner = rec["id"]
    if doc["winner"] != expect_winner:
        fail(f"gold24 winner {doc['winner']} != recomputed first-passing {expect_winner}")
    print(f"3. Pass criteria re-derived mechanically (V0 -> V3 -> V5): "
          f"winner = {expect_winner}. OK")


def check_60d_untouched() -> None:
    sys.path.insert(0, str(ROOT))
    matrix = json.loads((RESULTS / "experiments.json").read_text())
    rec = next(r for r in matrix["runs"] if r["id"] == "mgc_1m_v0_baseline")
    if rec["net_usd"] != 584.0 or rec["trades"] != 18 or rec["net_r"] != 2.0:
        fail(f"experiments.json MGC V0 changed: {rec['net_usd']} / {rec['trades']} trades")
    from fcr.backtest import run_backtest
    from fcr.config import load_config
    from fcr.data import load_csv
    cfg = load_config(ROOT / "config.yaml")
    cfg.update(symbol="MGC", instrument_type="futures",
               point_value_usd=10.0, max_units=5)
    df = load_csv(ROOT / "data" / "gc_1m_databento.csv")
    rows, days, meta = run_backtest(df, cfg)
    net = round(sum(r["pnl_usd"] for r in rows), 2)
    if len(rows) != 18 or net != 584.0 or days != 52:
        fail(f"60-day MGC V0 re-run disagrees: {len(rows)} trades, ${net}, {days} days")
    print("4. 60-day MGC V0 untouched: experiments.json still +$584.00 / 18 "
          "trades, and a fresh re-run reproduces it exactly. OK")


def main() -> int:
    doc = json.loads((RESULTS / "gold24.json").read_text())
    if len(doc["runs"]) != 3:
        fail(f"gold24.json has {len(doc['runs'])} runs, expected 3")
    recomputed = check_recompute(doc)
    check_v0_invariants(doc)
    check_verdict(doc, recomputed)
    check_60d_untouched()
    print("\nAll gold24 verification checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
