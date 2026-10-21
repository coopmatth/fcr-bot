#!/usr/bin/env python3
"""Aggregate the Strategy Lab matrix into results/strategy_lab.json.

Sources (all produced by the phase runners):
  * 35 intraday-pack runs  results/<strategy>_<symbol>_120d.json
    (orb15, orb30, orb15_close, pd_sweep, vwap_revert, vwap_trend,
    ib_break x MNQ/MES/MGC/SIL/SPY), window 2026-06-22 -> 2026-10-20
  * 5 FCR V0 reference runs results/fcr_v0_<symbol>_120d.json
    (run_fcr_lab.py, route proven against the -$374.50 MNQ baseline)
  * 5 daily runs in results/daily_strategies.json (donchian_trend on
    SPY/QQQ/GC, rsi2_reversion on SPY/QQQ; last 120 trading days)

Every run is normalized to one record shape and ranked twice: by
net-after-friction $ and by net R. Runs with < 15 trades are flagged
anecdote_class (a handful of trades is an anecdote, not a verdict).

Run:  .venv/bin/python build_strategy_lab.py
Then: .venv/bin/python verify_strategy_lab.py <run_id> [<run_id> ...]
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
ET = ZoneInfo("America/New_York")

INTRADAY_STRATEGIES = ["orb15", "orb30", "orb15_close", "pd_sweep",
                       "vwap_revert", "vwap_trend", "ib_break"]
SYMBOL_KEYS = ["mnq", "mes", "mgc", "sil", "spy"]
STARTING_BALANCE = 500.0


def base_record(rec: dict) -> dict:
    return {
        "id": rec["id"],
        "strategy": rec["strategy"],
        "strategy_label": rec["strategy_label"],
        "symbol": rec["symbol"],
        "symbol_key": rec["symbol_key"],
        "data_file": rec["data_file"],
        "point_value_usd": rec["point_value_usd"],
        "friction_per_unit_usd": rec["friction_per_unit_usd"],
        "trades": rec["trades"],
        "wins": rec["wins"],
        "losses": rec["losses"],
        "win_rate": rec["win_rate"],
        "net_usd": rec["net_usd"],
        "net_r": rec["net_r"],
        "net_after_friction_usd": rec["net_usd_after_friction"],
        "ending_equity_usd": rec["ending_equity_usd"],
        "max_drawdown_usd": rec["max_drawdown_usd"],
        "skipped_for_size": rec["skipped_for_size"],
        "trade_log": rec["trade_log"],
    }


def main() -> int:
    runs: list[dict] = []

    for strat in INTRADAY_STRATEGIES:
        for sym in SYMBOL_KEYS:
            rec = json.loads(
                (RESULTS / f"{strat}_{sym}_120d.json").read_text())
            r = base_record(rec)
            r["window"] = rec["window"]
            r["trading_days"] = rec["trading_days"]
            runs.append(r)

    for sym in SYMBOL_KEYS:
        rec = json.loads((RESULTS / f"fcr_v0_{sym}_120d.json").read_text())
        r = base_record(rec)
        r["window"] = rec["window"]
        r["trading_days"] = rec["trading_days"]
        runs.append(r)

    daily = json.loads((RESULTS / "daily_strategies.json").read_text())
    label = {"donchian_trend": "Donchian trend (20d/10d, 2xATR stop)",
             "rsi2_reversion": "Connors RSI(2) reversion"}
    for rec in daily["runs"]:
        net = rec["net_of_friction"]
        runs.append({
            "id": rec["id"],
            "strategy": rec["strategy"],
            "strategy_label": label[rec["strategy"]],
            "symbol": rec["symbol"],
            "symbol_key": rec["symbol"].lower(),
            "traded_as": rec["traded_as"],
            "data_file": rec["data_file"],
            "point_value_usd": rec["point_value_usd"],
            "friction_per_unit_usd":
                rec["friction_per_unit_round_trip_usd"],
            "window": {"start": rec["window"]["start"],
                       "end": rec["window"]["end"]},
            "trading_days": rec["window"]["trading_days"],
            "trades": rec["trades"],
            "wins": rec["wins"],
            "losses": rec["losses"],
            "win_rate": rec["win_rate"],
            "net_usd": rec["net_usd"],
            "net_r": rec["net_r"],
            "net_after_friction_usd": net["net_usd"],
            "ending_equity_usd": rec["ending_equity_usd"],
            "max_drawdown_usd": rec["max_drawdown_usd"],
            "skipped_for_size": rec["skipped_for_size"],
            "trade_log": rec["trade_log"],
        })

    for r in runs:
        r["anecdote_class"] = r["trades"] < 15

    by_money = sorted(runs, key=lambda r: -r["net_after_friction_usd"])
    by_r = sorted(runs, key=lambda r: -r["net_r"])
    for i, r in enumerate(by_money, 1):
        r["rank_net_after_friction"] = i
    for i, r in enumerate(by_r, 1):
        r["rank_net_r"] = i

    doc = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "window": {
            "intraday": "2026-06-22 -> 2026-10-20 ET (SPY data ends "
                        "2026-10-19, DBEQ.BASIC lag)",
            "daily": "last 120 trading days (2026-04-30 -> 2026-10-20)",
            "note": "120 calendar days of intraday data = 87 RTH "
                    "sessions (84 for SPY); the daily strategies trade "
                    "a 120-TRADING-day window, as built.",
        },
        "spec": {
            "starting_balance_usd": STARTING_BALANCE,
            "risk_budget_usd": 150,
            "friction_round_trip_per_unit_usd": {
                "MNQ": 3.40, "MES": 4.90, "MGC": 4.40, "SIL": 12.40,
                "SPY/QQQ shares": 0.04, "GC as MGC": 4.40,
            },
            "friction_model": "fixed per-unit round-trip charge "
                              "(fees + ~1 tick slippage/side) deducted "
                              "from every trade for the *_after_friction "
                              "figures; gross figures model no costs",
        },
        "runs": runs,
        "rankings": {
            "by_net_after_friction": [r["id"] for r in by_money],
            "by_net_r": [r["id"] for r in by_r],
        },
    }
    out = RESULTS / "strategy_lab.json"
    out.write_text(json.dumps(doc, indent=2))
    print(f"Wrote {out} ({len(runs)} runs)")
    print("\nTop 10 by net-after-friction $:")
    for r in by_money[:10]:
        print(f"  {r['id']:28s} {r['trades']:3d}t "
              f"net ${r['net_usd']:+9.2f}  after-friction "
              f"${r['net_after_friction_usd']:+9.2f}  {r['net_r']:+.2f}R")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
