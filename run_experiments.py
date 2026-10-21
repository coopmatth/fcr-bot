#!/usr/bin/env python3
"""FCR experiment matrix: 6 variants x 5 symbols on Databento 1-minute
data (paper only). Answers one question: do principled adjustments make
the First Candle Rule profitable, and on which markets?

Data (all in data/, fetched 2026-10-21; raw CSVs are NOT in the repo):
  * mnq_1m_databento.csv - MNQ.v.0 tape (GLBX.MDP3), traded as MNQ $2/pt
  * es_1m_databento.csv  - ES.v.0 tape, traded as MES $5/pt
  * gc_1m_databento.csv  - GC.v.0 tape, traded as MGC $10/pt
  * si_1m_databento.csv  - SI.v.0 tape, traded as SIL $1000/pt
  * spy_1m_databento.csv - SPY (DBEQ.BASIC), shares; window ends
    2026-10-19, one day shorter than the futures (dataset lag)

Futures are priced off the full-size contract's tape and sized as the
micro contract (same price series; only the point value differs).

Common spec for every run: $500 starting equity, $150 risk budget per
trade, futures capped at 5 contracts (min(5, floor(150 / (stop x
point value)))); SPY in uncapped shares (floor(150 / stop $)); one
trade/day; entries before 12:00 ET; flatten 15:55 ET; same-bar
stop+target -> stop first; NO slippage/spread/commissions modeled.

Variants (frozen definitions, identical for every symbol):
  V0 baseline  - the original rules, exactly as shipped
  V1 min_fvg   - FVG height >= 0.5 x ATR(14) of today's RTH 1m bars
                 before the displacement candle (min 5 bars, else void)
  V2 fc_filter - skip the day when the first-candle range exceeds
                 1.5 x the median first-candle range of all PRIOR days
                 in the dataset (needs >= 5 prior days)
  V3 partials  - at +1.5R: bank half the position (floor) at that price
                 (1-unit positions: no bank) and move the stop to
                 breakeven; remainder exits at +3R / moved stop / 15:55
  V4 trend     - setups only in the direction of the first candle's
                 body (close vs open; a flat body allows both)
  V5 combined  - V1 + V2 + V3 + V4 together

Outputs: results/trades_<run_id>.csv for all 30 runs and the
machine-readable matrix results/experiments.json.

Run:  .venv/bin/python run_experiments.py
Then: .venv/bin/python verify_experiments.py  (independent checks)
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from fcr.backtest import TRADE_LOG_COLUMNS, run_backtest, run_stats
from fcr.config import load_config
from fcr.data import load_csv

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

EXTENDED_COLUMNS = TRADE_LOG_COLUMNS + [
    "partial_units",
    "partial_exit_time",
    "partial_exit_price",
    "partial_pnl_usd",
]

# key, display label, csv, data symbol note, traded-as symbol,
# point value $/pt, max units (None = uncapped shares), instrument type
SYMBOLS = [
    ("mnq", "MNQ", "mnq_1m_databento.csv", "MNQ.v.0 (GLBX.MDP3)", "MNQ", 2.0, 5, "futures"),
    ("mes", "MES", "es_1m_databento.csv", "ES.v.0 tape (GLBX.MDP3)", "MES", 5.0, 5, "futures"),
    ("mgc", "MGC", "gc_1m_databento.csv", "GC.v.0 tape (GLBX.MDP3)", "MGC", 10.0, 5, "futures"),
    ("sil", "SIL", "si_1m_databento.csv", "SI.v.0 tape (GLBX.MDP3)", "SIL", 1000.0, 5, "futures"),
    ("spy", "SPY", "spy_1m_databento.csv", "SPY (DBEQ.BASIC)", "SPY", 1.0, None, "equity"),
]

# key, name suffix, label, engine config overrides (fc_filter is handled
# by the per-day hook below, not an engine key)
VARIANTS = [
    ("v0", "baseline", "V0 baseline", {}),
    ("v1", "min_fvg", "V1 min-FVG", {"min_fvg_atr_mult": 0.5}),
    ("v2", "fc_filter", "V2 first-candle filter", {"fc_filter": True}),
    ("v3", "partials", "V3 partials+BE", {"partials": True}),
    ("v4", "trend", "V4 trend filter", {"trend_filter": True}),
    ("v5", "combined", "V5 combined (V1+V2+V3+V4)", {
        "min_fvg_atr_mult": 0.5,
        "fc_filter": True,
        "partials": True,
        "trend_filter": True,
    }),
]

FC_START, FC_END = time(9, 30), time(9, 35)


def first_candle_ranges(df: pd.DataFrame) -> dict[str, float]:
    """Per-date first-candle (09:30-09:35 ET) range from session bars."""
    session = df[
        (df["timestamp"].dt.time >= FC_START) & (df["timestamp"].dt.time < FC_END)
    ]
    ranges: dict[str, float] = {}
    for date, day in session.groupby(session["timestamp"].dt.date):
        ranges[date.isoformat()] = float(day["high"].max() - day["low"].min())
    return ranges


def fc_limit_hook(ranges: dict[str, float]):
    """Build the run_backtest cfg_for_day hook for the V2 filter.

    For each day, the limit is 1.5 x the median first-candle range of
    all PRIOR days present in the dataset; with fewer than 5 prior days
    there is no limit (no skip). Days without first-candle bars are not
    in `ranges` and never receive a limit.
    """
    ordered = sorted(ranges)

    def hook(date_iso: str, cfg: dict) -> dict:
        prior = [ranges[d] for d in ordered if d < date_iso]
        if len(prior) < 5 or date_iso not in ranges:
            return cfg
        day_cfg = dict(cfg)
        day_cfg["fc_range_limit"] = 1.5 * statistics.median(prior)
        return day_cfg

    return hook


def main() -> int:
    base_cfg = load_config(ROOT / "config.yaml")
    records: list[dict] = []

    for key, label, csv_name, data_symbol, traded_as, pv, cap, itype in SYMBOLS:
        df = load_csv(ROOT / "data" / csv_name)
        hook = fc_limit_hook(first_candle_ranges(df))
        sym_cfg = dict(base_cfg)
        sym_cfg.update(
            symbol=traded_as,
            instrument_type=itype,
            point_value_usd=pv,
            max_units=cap,
        )
        for vkey, vname, vlabel, overrides in VARIANTS:
            cfg = dict(sym_cfg)
            cfg.update(overrides)
            use_hook = hook if overrides.get("fc_filter") else None
            rows, days, meta = run_backtest(df, cfg, cfg_for_day=use_hook)
            stats = run_stats(rows, days, meta)

            run_id = f"{key}_1m_{vkey}_{vname}"
            log_name = f"trades_{run_id}.csv"
            out = ROOT / "results" / log_name
            pd.DataFrame(rows, columns=EXTENDED_COLUMNS).to_csv(out, index=False)

            record = {
                "id": run_id,
                "label": f"{label} 1m · {vlabel}",
                "symbol": traded_as,
                "symbol_key": key,
                "variant": vkey,
                "variant_name": vname,
                "variant_label": vlabel,
                "data_file": f"data/{csv_name}",
                "data_symbol": data_symbol,
                "instrument_type": itype,
                "point_value_usd": pv,
                "date_range": {
                    "start": df["timestamp"].min().date().isoformat(),
                    "end": df["timestamp"].max().date().isoformat(),
                },
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
                "trade_log": f"results/{log_name}",
            }
            records.append(record)
            print(
                f"[{run_id}] {days} days, {stats['trades']} trades "
                f"({stats['wins']}W/{stats['losses']}L), "
                f"net ${stats['net_usd']:+.2f} / {stats['net_r']:+.2f}R, "
                f"equity -> {stats['ending_equity_usd']}, "
                f"maxDD ${stats['max_drawdown_usd']}, "
                f"skipped {stats['skipped_for_size']}"
            )

    matrix = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "execution_model": (
            "Paper simulation only. Entries fill at the engulfing bar's "
            "close; exits at stop/target (V3 partial leg at the +1.5R "
            "price); NO slippage, spread, or commissions modeled. The "
            "simulator never halts for balance or margin."
        ),
        "spec": {
            "starting_balance_usd": 500,
            "risk_budget_usd": 150,
            "futures_max_contracts": 5,
            "equity_sizing": "uncapped shares = floor(150 / stop $)",
            "rr_target": 3.0,
            "one_trade_per_day": True,
            "entry_cutoff": "12:00 ET",
            "flatten_time": "15:55 ET",
            "window": "2026-08-10 -> 2026-10-20 (SPY -> 2026-10-19)",
        },
        "variants": {v[0]: v[2] for v in VARIANTS},
        "runs": records,
    }
    out = ROOT / "results" / "experiments.json"
    out.write_text(json.dumps(matrix, indent=2))
    print(f"\nMatrix written to {out} ({len(records)} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
