#!/usr/bin/env python3
"""FCR V0 reference runs for the Strategy Lab (paper only).

The lab matrix (results/strategy_lab.json) includes the original First
Candle Rule (V0 baseline config, exactly as shipped) as the reference
column, run on the SAME 120-day window and data as the intraday
strategy pack: 2026-06-22 -> 2026-10-20 ET.

Route proof (runs first, aborts the matrix on failure): FCR V0 on MNQ
restricted to 2026-08-10 -> 2026-10-20 on data/mnq_1m_120d.csv must
reproduce the known Databento baseline EXACTLY — 13 trades, net
-$374.50 (see results/trades_mnq_1m_databento_60d.csv /
verify_experiments.py). Only a route that reproduces it is trusted for
the lab window.

Sizing/config mirror run_experiments.py (load_config(config.yaml) +
per-symbol overrides; V0 = no variant overrides, no per-day hook; the
engine is day-local for V0, so slicing the frame to the window is
exact). Friction twins use the intraday pack's per-unit round-trip
assumptions (MNQ $3.40, MES $4.90, MGC $4.40, SIL $12.40, SPY $0.04).

Outputs: results/trades_fcr_v0_<symbol>_120d.csv and
results/fcr_v0_<symbol>_120d.json per symbol.

Run:  .venv/bin/python run_fcr_lab.py [--proof-only]
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from fcr.backtest import TRADE_LOG_COLUMNS, run_backtest, run_stats
from fcr.config import load_config
from fcr.data import load_csv

ROOT = Path(__file__).resolve().parent

EXTENDED_COLUMNS = TRADE_LOG_COLUMNS + [
    "partial_units", "partial_exit_time", "partial_exit_price",
    "partial_pnl_usd",
]

# key, traded symbol, csv, data symbol note, point value, max units,
# instrument type, friction $/unit round trip
SYMBOLS = [
    ("mnq", "MNQ", "mnq_1m_120d.csv", "MNQ.v.0 (GLBX.MDP3)", 2.0, 5, "futures", 3.40),
    ("mes", "MES", "es_1m_120d.csv", "ES.v.0 tape (GLBX.MDP3)", 5.0, 5, "futures", 4.90),
    ("mgc", "MGC", "gc_1m_24mo.csv", "GC.v.0 tape (GLBX.MDP3)", 10.0, 5, "futures", 4.40),
    ("sil", "SIL", "si_1m_120d.csv", "SI.v.0 tape (GLBX.MDP3)", 1000.0, 5, "futures", 12.40),
    ("spy", "SPY", "spy_1m_120d.csv", "SPY (DBEQ.BASIC)", 1.0, None, "equity", 0.04),
]

LAB_START, LAB_END = date(2026, 6, 22), date(2026, 10, 20)
PROOF_START, PROOF_END = date(2026, 8, 10), date(2026, 10, 20)


def slice_window(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    d = df["timestamp"].dt.date
    return df[(d >= start) & (d <= end)].reset_index(drop=True)


def run_v0(df: pd.DataFrame, traded: str, pv: float, cap, itype: str):
    cfg = dict(load_config(ROOT / "config.yaml"))
    cfg.update(symbol=traded, instrument_type=itype,
               point_value_usd=pv, max_units=cap)
    rows, days, meta = run_backtest(df, cfg)
    return rows, days, run_stats(rows, days, meta)


def proof() -> bool:
    spec = next(s for s in SYMBOLS if s[0] == "mnq")
    _, traded, csv_name, _, pv, cap, itype, _ = spec
    df = load_csv(ROOT / "data" / csv_name)
    window = slice_window(df, PROOF_START, PROOF_END)
    _, _, stats = run_v0(window, traded, pv, cap, itype)
    ok = stats["trades"] == 13 and stats["net_usd"] == -374.50
    print(f"PROOF FCR V0 MNQ {PROOF_START} -> {PROOF_END}: "
          f"{stats['trades']} trades, net ${stats['net_usd']:+.2f} "
          f"(expected 13 / -$374.50) -> {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    if not proof():
        print("Route proof FAILED — matrix aborted.", file=sys.stderr)
        return 1
    if "--proof-only" in sys.argv:
        return 0
    for key, traded, csv_name, data_symbol, pv, cap, itype, friction in SYMBOLS:
        df = load_csv(ROOT / "data" / csv_name)
        window = slice_window(df, LAB_START, LAB_END)
        rows, days, stats = run_v0(window, traded, pv, cap, itype)
        run_id = f"fcr_v0_{key}_120d"
        log_name = f"trades_{run_id}.csv"
        pd.DataFrame(rows, columns=EXTENDED_COLUMNS).to_csv(
            ROOT / "results" / log_name, index=False)
        total_friction = round(
            sum(r["contracts"] for r in rows) * friction, 2)
        net_after = round(stats["net_usd"] - total_friction, 2)
        record = {
            "id": run_id,
            "label": f"{traded} 1m · FCR V0 baseline",
            "strategy": "fcr_v0",
            "strategy_label": "First Candle Rule V0 baseline (reference)",
            "symbol": traded,
            "symbol_key": key,
            "data_file": f"data/{csv_name}",
            "data_symbol": data_symbol,
            "instrument_type": itype,
            "point_value_usd": pv,
            "friction_per_unit_usd": friction,
            "window": {"start": LAB_START.isoformat(),
                       "end": LAB_END.isoformat()},
            "trading_days": stats["trading_days"],
            "trades": stats["trades"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate": stats["win_rate"],
            "net_usd": stats["net_usd"],
            "net_r": stats["net_r"],
            "starting_balance_usd": stats["starting_balance_usd"],
            "ending_equity_usd": stats["ending_equity_usd"],
            "max_drawdown_usd": stats["max_drawdown_usd"],
            "avg_units": stats["avg_contracts"],
            "skipped_for_size": stats["skipped_for_size"],
            "total_friction_usd": total_friction,
            "net_usd_after_friction": net_after,
            "ending_equity_usd_after_friction":
                round(stats["starting_balance_usd"] + net_after, 2),
            "trade_log": f"results/{log_name}",
        }
        (ROOT / "results" / f"{run_id}.json").write_text(
            json.dumps(record, indent=2))
        print(f"[{run_id}] {days} days, {stats['trades']} trades "
              f"({stats['wins']}W/{stats['losses']}L), "
              f"net ${stats['net_usd']:+.2f} / {stats['net_r']:+.2f}R "
              f"(after friction ${net_after:+.2f}), "
              f"maxDD ${stats['max_drawdown_usd']}, "
              f"skipped {stats['skipped_for_size']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
