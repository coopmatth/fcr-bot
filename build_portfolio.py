#!/usr/bin/env python3
"""Conjunction portfolio: the lab's top cells in ONE $500 account.

Selection (frozen rule): rank all lab runs by net-after-friction $;
among runs with >= 15 trades, take the top 3 with DISTINCT strategy
families (orb15 / orb30 / orb15_close count as one ORB family) — a
conjunction of three near-identical ORB cells would be one bet, not
three.

Combination mechanics: every component trade keeps its own strategy's
recorded size and net-of-friction pnl. Trades are processed
chronologically by entry time (daily-strategy trades enter at their
entry date 09:30 ET and exit at their exit date 16:00 ET). A shared
$300 cap on summed OPEN initial risk applies: at a trade's entry, if
the open-risk sum plus this trade's initial risk would exceed $300,
the trade is skipped (and counted); a trade's risk stops counting the
moment it exits. Exits at the same timestamp are processed before
entries (they free risk first).

Simplification (stated, not hidden): the equity curve is built from
REALIZED pnl at exit times only — no mark-to-market while trades are
open — so the reported max drawdown is the drawdown of the realized
curve, and concurrent trades' interim swings are invisible to it.

Outputs: results/portfolio.json and
results/trades_portfolio_combined.csv (every considered trade, taken
or skipped-by-cap).

Run:  .venv/bin/python build_portfolio.py
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
ET = ZoneInfo("America/New_York")
STARTING_BALANCE = 500.0
RISK_CAP_USD = 300.0
MIN_TRADES = 15


def family(strategy: str) -> str:
    return "orb" if strategy.startswith("orb") else strategy


def load_component_trades(rec: dict) -> list[dict]:
    """Normalize one component run's trade CSV to
    {entry_dt, exit_dt, risk_usd, net_pnl_usd, direction}."""
    path = ROOT / rec["trade_log"]
    with path.open() as fh:
        rows = list(csv.DictReader(fh))
    out = []
    if rec["trade_log"].endswith("_daily.csv"):
        for r in rows:
            out.append({
                "entry_dt": f"{r['entry_date']} 09:30",
                "exit_dt": f"{r['exit_date']} 16:00",
                "risk_usd": float(r["risk_usd"]),
                "net_pnl_usd": float(r["pnl_net_usd"]),
                "direction": r["direction"],
            })
    else:
        pv = rec["point_value_usd"]
        for r in rows:
            units = float(r["contracts"])
            risk = abs(float(r["entry_price"]) - float(r["stop"])) \
                * units * pv
            if "friction_usd" in r:
                friction = float(r["friction_usd"])
            else:
                friction = units * rec["friction_per_unit_usd"]
            out.append({
                "entry_dt": f"{r['date']} {r['entry_time']}",
                "exit_dt": f"{r['date']} {r['exit_time']}",
                "risk_usd": round(risk, 2),
                "net_pnl_usd": round(float(r["pnl_usd"]) - friction, 2),
                "direction": r["direction"],
            })
    return out


def main() -> int:
    lab = json.loads((RESULTS / "strategy_lab.json").read_text())
    eligible = [r for r in lab["runs"] if r["trades"] >= MIN_TRADES]
    eligible.sort(key=lambda r: -r["net_after_friction_usd"])
    picked: list[dict] = []
    seen: set[str] = set()
    for r in eligible:
        if family(r["strategy"]) in seen:
            continue
        seen.add(family(r["strategy"]))
        picked.append(r)
        if len(picked) == 3:
            break

    # Build the event stream: exits (kind 0) before entries (kind 1)
    # at equal timestamps; entries tie-broken by run id, then index.
    events = []
    components = []
    for rec in picked:
        trades = load_component_trades(rec)
        components.append({
            "run_id": rec["id"], "strategy": rec["strategy"],
            "symbol": rec["symbol"],
            "standalone": {
                "trades": rec["trades"],
                "net_usd": rec["net_usd"],
                "net_r": rec["net_r"],
                "net_after_friction_usd": rec["net_after_friction_usd"],
                "max_drawdown_usd": rec["max_drawdown_usd"],
            },
            "trades_considered": len(trades),
            "trades_taken": 0, "trades_skipped_by_cap": 0,
            "taken_net_usd": 0.0,
        })
        for i, t in enumerate(trades):
            key = (rec["id"], i)
            events.append((t["entry_dt"], 1, rec["id"], i, "entry", key, t))
            events.append((t["exit_dt"], 0, rec["id"], i, "exit", key, t))
    events.sort(key=lambda e: (e[0], e[1], e[2], e[3]))

    open_risk: dict = {}
    taken: set = set()
    combined_rows = []
    equity = STARTING_BALANCE
    peak = STARTING_BALANCE
    max_dd = 0.0
    comp_by_id = {c["run_id"]: c for c in components}
    for _, _, run_id, _, kind, key, t in events:
        if kind == "entry":
            if sum(open_risk.values()) + t["risk_usd"] <= RISK_CAP_USD + 1e-9:
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
        else:  # exit
            if key in taken:
                open_risk.pop(key, None)
                equity += t["net_pnl_usd"]
                comp_by_id[run_id]["taken_net_usd"] = round(
                    comp_by_id[run_id]["taken_net_usd"]
                    + t["net_pnl_usd"], 2)
                peak = max(peak, equity)
                max_dd = max(max_dd, peak - equity)

    combined_rows.sort(key=lambda r: (r["entry_dt"], r["run_id"]))
    taken_rows = [r for r in combined_rows if r["taken"]]
    net = round(sum(r["net_pnl_usd"] for r in taken_rows), 2)
    wins = sum(1 for r in taken_rows if r["net_pnl_usd"] > 0)
    naive = round(sum(c["standalone"]["net_after_friction_usd"]
                      for c in components), 2)

    doc = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "selection_rule": (
            "Top 3 lab cells by net-after-friction $ among runs with "
            f">= {MIN_TRADES} trades, at most one cell per strategy "
            "family (orb15/orb30/orb15_close = one ORB family)."),
        "risk_cap_usd": RISK_CAP_USD,
        "starting_balance_usd": STARTING_BALANCE,
        "components": components,
        "combined": {
            "trades_considered": len(combined_rows),
            "trades_taken": len(taken_rows),
            "trades_skipped_by_cap":
                sum(c["trades_skipped_by_cap"] for c in components),
            "wins": wins,
            "losses": len(taken_rows) - wins,
            "win_rate": round(wins / len(taken_rows), 4)
            if taken_rows else None,
            "net_usd": net,
            "ending_equity_usd": round(STARTING_BALANCE + net, 2),
            "max_drawdown_usd": round(max_dd, 2),
            "naive_uncapped_sum_usd": naive,
        },
        "simplifications": (
            "Equity curve uses realized pnl at exit times only (no "
            "mark-to-market of open trades), so max drawdown is the "
            "realized-curve drawdown. Daily-strategy trades are "
            "timestamped entry-date 09:30 ET / exit-date 16:00 ET. "
            "Each trade keeps its own strategy's recorded size; the "
            "only portfolio-level constraint is the $300 open-risk "
            "cap."),
        "trade_log": "results/trades_portfolio_combined.csv",
    }
    (RESULTS / "portfolio.json").write_text(json.dumps(doc, indent=2))
    cols = ["run_id", "strategy", "symbol", "direction", "entry_dt",
            "exit_dt", "risk_usd", "net_pnl_usd", "taken", "skip_reason"]
    with (RESULTS / "trades_portfolio_combined.csv").open(
            "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(combined_rows)

    print("Components:")
    for c in components:
        print(f"  {c['run_id']:28s} standalone after-friction "
              f"${c['standalone']['net_after_friction_usd']:+9.2f}; "
              f"taken {c['trades_taken']}/{c['trades_considered']}, "
              f"skipped-by-cap {c['trades_skipped_by_cap']}, "
              f"taken net ${c['taken_net_usd']:+.2f}")
    cb = doc["combined"]
    print(f"Combined: {cb['trades_taken']} taken / "
          f"{cb['trades_skipped_by_cap']} skipped-by-cap, "
          f"net ${cb['net_usd']:+.2f}, WR {cb['win_rate']}, "
          f"equity -> ${cb['ending_equity_usd']:.2f}, "
          f"maxDD ${cb['max_drawdown_usd']:.2f} "
          f"(naive uncapped sum ${naive:+.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
