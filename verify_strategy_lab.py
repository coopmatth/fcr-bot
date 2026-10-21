#!/usr/bin/env python3
"""Independent verification of Strategy Lab runs.

Recomputes every headline aggregate from the TRADE CSVs only (never
the runners' JSON stats): trades, W/L, win rate, net $, net R,
net-after-friction $, ending equity, and max drawdown (trade-order
equity path from the $500 start — identical to the runners' day-order
path here because every strategy trades at most one position at a
time and closes it before the next). Compares against
results/strategy_lab.json and exits non-zero on any mismatch.

Usage: .venv/bin/python verify_strategy_lab.py <run_id> [<run_id> ...]
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
TOL_USD = 0.02
TOL_R = 0.01


def load_rows(path: Path) -> list[dict]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def f(row: dict, key: str) -> float:
    return float(row[key])


def recompute(rec: dict) -> dict:
    rows = load_rows(ROOT / rec["trade_log"])
    if rec["trade_log"].endswith("_daily.csv"):
        pnls = [f(r, "pnl_usd") for r in rows]
        net_after = round(sum(f(r, "pnl_net_usd") for r in rows), 2)
        rs = [f(r, "r_multiple") for r in rows]
    else:
        pnls = [f(r, "pnl_usd") for r in rows]
        rs = [f(r, "r_multiple") for r in rows]
        if rows and "friction_usd" in rows[0]:
            friction = sum(f(r, "friction_usd") for r in rows)
        else:  # FCR logs carry no friction column; recompute per unit
            friction = sum(f(r, "contracts") for r in rows) \
                * rec["friction_per_unit_usd"]
        net_after = round(sum(pnls) - friction, 2)
    net = round(sum(pnls), 2)
    equity = peak = 500.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": round(wins / len(rows), 4) if rows else None,
        "net_usd": net,
        "net_r": round(sum(rs), 3),
        "net_after_friction_usd": net_after,
        "ending_equity_usd": round(500.0 + net, 2),
        "max_drawdown_usd": round(max_dd, 2),
    }


def main(argv: list[str]) -> int:
    lab = json.loads((RESULTS / "strategy_lab.json").read_text())
    by_id = {r["id"]: r for r in lab["runs"]}
    failures = 0
    for run_id in argv:
        rec = by_id[run_id]
        got = recompute(rec)
        bad = []
        for key, val in got.items():
            want = rec[key]
            if val is None or want is None:
                if val != want:
                    bad.append(f"{key}: csv {val} != lab {want}")
            elif abs(val - want) > (TOL_R if key in ("net_r", "win_rate")
                                    else TOL_USD):
                bad.append(f"{key}: csv {val} != lab {want}")
        if bad:
            failures += 1
            print(f"FAIL {run_id}: " + "; ".join(bad))
        else:
            print(f"OK   {run_id}: {got['trades']} trades, "
                  f"net ${got['net_usd']:+.2f} / {got['net_r']:+.2f}R, "
                  f"after friction ${got['net_after_friction_usd']:+.2f}, "
                  f"maxDD ${got['max_drawdown_usd']:.2f} — all recompute "
                  f"from the trade CSV")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
