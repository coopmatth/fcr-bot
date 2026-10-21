#!/usr/bin/env python3
"""FCR gold 24-month validation (paper only).

The 60-day experiment matrix (run_experiments.py) left one hypothesis:
gold (MGC) was the only symbol positive under the untouched baseline
and under most adjustments. This script tests that hypothesis on 24
months of 1-minute GC data with the FROZEN variant definitions — no
retuning: V0 baseline, V3 partials+BE, V5 combined, identical config
machinery to the matrix (imports VARIANTS / the fc-filter hook from
run_experiments.py).

Data: data/gc_1m_24mo.csv — GC.v.0 continuous tape (Databento
GLBX.MDP3, ohlcv-1m), 2024-10-21 -> 2026-10-20 ET, fetched
2026-10-21 for $2.56 of account credit. Traded as MGC at $10/pt,
$500 starting equity, $150 risk budget, <= 5 contracts, one trade/day,
entries before 12:00 ET, flatten 15:55 ET, NO slippage/fees modeled —
the exact spec of the 60-day runs.

Outputs: results/trades_mgc_1m_24mo_<variant>.csv and
results/gold24.json with per-variant totals AND half splits
(H1 2024-10-21..2025-10-20, H2 2025-10-21..2026-10-20), equity trough,
max drawdown, longest losing streak, a data-quality note (RTH days
with < 300 session bars), and the mechanical pass/fail verdict.

PASS CRITERIA (frozen before the run, applied mechanically in the
order V0 -> V3 -> V5; the first passing variant wins):
  (a) 24-month net $ > 0 AND net R > 0;
  (b) the second half is also net positive (net $ > 0 AND net R > 0);
  (c) the simulated day-end equity trough stays above $0 (a real $500
      account would have survived the whole run).
If no variant passes, no bot is built.

Run:  .venv/bin/python run_gold24.py
Then: .venv/bin/python verify_gold24.py  (independent checks)
"""
from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from fcr.backtest import max_drawdown_usd, run_backtest, run_stats
from fcr.config import load_config
from fcr.data import load_csv
from fcr.engine import FCREngine
from fcr.patterns import Bar
from run_experiments import EXTENDED_COLUMNS, VARIANTS, fc_limit_hook, first_candle_ranges

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

DATA_FILE = "gc_1m_24mo.csv"
SESSION_START, SESSION_END = time(9, 30), time(16, 0)
HALF_SPLIT = "2025-10-20"  # H1: dates <= this; H2: dates > this
RUN_VARIANTS = ("v0", "v3", "v5")


def rth_day_bar_counts(df: pd.DataFrame) -> dict[str, int]:
    """Session (09:30-16:00 ET) bar count per calendar date."""
    session = df[
        (df["timestamp"].dt.time >= SESSION_START)
        & (df["timestamp"].dt.time < SESSION_END)
    ]
    counts = session.groupby(session["timestamp"].dt.date).size()
    return {d.isoformat(): int(n) for d, n in counts.items()}


def run_tracked(df: pd.DataFrame, cfg: dict, cfg_for_day=None):
    """run_backtest's exact day loop, but keeps per-day records so the
    totals can be split into halves without re-running (the V2/V5
    fc-filter hook depends on ALL prior days, so a split re-run would
    not reproduce the full run).

    Returns (rows, per_day, meta) where per_day is a list of
    {date, trades: [row...], skipped, equity} in chronological order
    and meta mirrors run_backtest's meta dict.
    """
    starting_balance = cfg.get("starting_balance_usd")
    rows: list[dict] = []
    per_day: list[dict] = []
    days = 0
    skipped = 0
    equity = float(starting_balance)
    equity_curve = [{"date": "start", "equity": round(equity, 2)}]
    df = df.copy()
    df["_date"] = df["timestamp"].dt.date
    for date, day_df in df.groupby("_date"):
        session = day_df[
            (day_df["timestamp"].dt.time >= SESSION_START)
            & (day_df["timestamp"].dt.time < SESSION_END)
        ]
        if session.empty:
            continue
        days += 1
        day_cfg = cfg_for_day(date.isoformat(), cfg) if cfg_for_day else cfg
        engine = FCREngine(day_cfg)
        engine.start_day(date.isoformat())
        last_bar = None
        for ts, o, h, l, c, v in zip(
            session["timestamp"],
            session["open"],
            session["high"],
            session["low"],
            session["close"],
            session["volume"],
        ):
            last_bar = Bar(
                ts=ts.to_pydatetime(),
                open=float(o), high=float(h), low=float(l),
                close=float(c), volume=float(v),
            )
            engine.on_bar(last_bar)
        if engine.position is not None and last_bar is not None:
            engine.force_close(last_bar.ts, last_bar.close, reason="DATA_END")
        day_rows = [t.as_row() for t in engine.trades]
        skipped += engine.skipped_for_size
        equity += sum(t.pnl_usd for t in engine.trades)
        rows.extend(day_rows)
        equity_curve.append({"date": date.isoformat(), "equity": round(equity, 2)})
        per_day.append({
            "date": date.isoformat(),
            "trades": day_rows,
            "skipped": engine.skipped_for_size,
            "equity": round(equity, 2),
        })
    meta = {
        "skipped_for_size": skipped,
        "starting_balance_usd": starting_balance,
        "ending_equity_usd": round(equity, 2),
        "max_drawdown_usd": max_drawdown_usd(equity_curve),
        "equity_curve": equity_curve,
    }
    return rows, days, meta, per_day


def longest_losing_streak(rows: list[dict]) -> int:
    best = cur = 0
    for r in rows:
        if r["pnl_usd"] < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def segment_stats(day_records: list[dict], opening_equity: float) -> dict:
    """Stats over a slice of per-day records (a half, or the full run).

    Drawdown/trough use the day-end equity curve seeded with the
    segment's opening equity — the same convention as run_stats.
    """
    rows = [r for d in day_records for r in d["trades"]]
    curve = [{"date": "start", "equity": round(opening_equity, 2)}]
    equity = opening_equity
    for d in day_records:
        equity = d["equity"]
        curve.append({"date": d["date"], "equity": equity})
    wins = sum(1 for r in rows if r["pnl_usd"] > 0)
    trough_point = min(curve, key=lambda p: p["equity"])
    return {
        "trading_days": len(day_records),
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": round(wins / len(rows), 4) if rows else None,
        "net_usd": round(sum(r["pnl_usd"] for r in rows), 2),
        "net_r": round(sum(r["r_multiple"] for r in rows), 3),
        "ending_equity_usd": round(equity, 2),
        "max_drawdown_usd": max_drawdown_usd(curve),
        "equity_trough_usd": trough_point["equity"],
        "equity_trough_date": trough_point["date"],
        "longest_losing_streak": longest_losing_streak(rows),
        "skipped_for_size": sum(d["skipped"] for d in day_records),
        "avg_units": round(sum(r["contracts"] for r in rows) / len(rows), 2) if rows else None,
    }


def main() -> int:
    base_cfg = load_config(ROOT / "config.yaml")
    df = load_csv(ROOT / "data" / DATA_FILE)

    counts = rth_day_bar_counts(df)
    short_days = [
        {"date": d, "rth_bars": n} for d, n in sorted(counts.items()) if n < 300
    ]
    databento_flagged = ["2025-09-17", "2025-09-24", "2025-11-28"]
    data_quality = {
        "rth_trading_days": len(counts),
        "full_rth_bars": 390,
        "short_day_threshold": 300,
        "short_days": short_days,
        "databento_degraded_flag_dates": databento_flagged,
        "note": (
            "Days with fewer than 300 RTH (09:30-16:00 ET) bars. Some are "
            "scheduled early closes (e.g. day after Thanksgiving, Christmas "
            "Eve, Jul 3 — a full early-close session is ~210 bars); Databento "
            "separately flagged 2025-09-17, 2025-09-24 and 2025-11-28 as "
            "degraded quality. Short days were NOT dropped: every day with "
            "session bars ran through the engine on whatever bars exist."
        ),
    }
    print(f"Data: {len(df):,} bars, {len(counts)} RTH days, "
          f"{len(short_days)} short days (<300 RTH bars)")

    hook = fc_limit_hook(first_candle_ranges(df))
    sym_cfg = dict(base_cfg)
    sym_cfg.update(
        symbol="MGC", instrument_type="futures",
        point_value_usd=10.0, max_units=5,
    )

    variants = {v[0]: v for v in VARIANTS}
    records: list[dict] = []
    for vkey in RUN_VARIANTS:
        _, vname, vlabel, overrides = variants[vkey]
        cfg = dict(sym_cfg)
        cfg.update(overrides)
        use_hook = hook if overrides.get("fc_filter") else None
        rows, days, meta, per_day = run_tracked(df, cfg, cfg_for_day=use_hook)

        # Cross-check the tracked driver against run_backtest itself:
        # identical trade rows and aggregates, or something drifted.
        ref_rows, ref_days, ref_meta = run_backtest(df, cfg, cfg_for_day=use_hook)
        if ref_rows != rows or ref_days != days or \
                ref_meta["ending_equity_usd"] != meta["ending_equity_usd"] or \
                ref_meta["max_drawdown_usd"] != meta["max_drawdown_usd"] or \
                ref_meta["skipped_for_size"] != meta["skipped_for_size"]:
            raise SystemExit(f"{vkey}: tracked driver disagrees with run_backtest")
        stats = run_stats(rows, days, meta)

        run_id = f"mgc_1m_24mo_{vkey}_{vname}"
        log_name = f"trades_{run_id}.csv"
        pd.DataFrame(rows, columns=EXTENDED_COLUMNS).to_csv(
            ROOT / "results" / log_name, index=False)

        h1_days = [d for d in per_day if d["date"] <= HALF_SPLIT]
        h2_days = [d for d in per_day if d["date"] > HALF_SPLIT]
        total = segment_stats(per_day, 500.0)
        # Headline numbers come from run_stats (same code path as the
        # 60-day matrix); segment_stats must agree with it.
        assert total["net_usd"] == stats["net_usd"] and \
            total["ending_equity_usd"] == stats["ending_equity_usd"] and \
            total["max_drawdown_usd"] == stats["max_drawdown_usd"], vkey
        half1 = segment_stats(h1_days, 500.0)
        half2 = segment_stats(h2_days, half1["ending_equity_usd"])

        checks = {
            "a_full_run_positive": total["net_usd"] > 0 and total["net_r"] > 0,
            "b_second_half_positive": half2["net_usd"] > 0 and half2["net_r"] > 0,
            "c_trough_above_zero": total["equity_trough_usd"] > 0,
        }
        record = {
            "id": run_id,
            "label": f"MGC 1m 24mo · {vlabel}",
            "symbol": "MGC",
            "variant": vkey,
            "variant_name": vname,
            "variant_label": vlabel,
            "data_file": f"data/{DATA_FILE}",
            "data_symbol": "GC.v.0 (GLBX.MDP3)",
            "instrument_type": "futures",
            "point_value_usd": 10.0,
            "date_range": {
                "start": df["timestamp"].min().date().isoformat(),
                "end": df["timestamp"].max().date().isoformat(),
            },
            **total,
            "halves": {
                "first": {"range": "2024-10-21..2025-10-20", **half1},
                "second": {"range": "2025-10-21..2026-10-20", **half2},
            },
            "pass_checks": checks,
            "passed": all(checks.values()),
            "trade_log": f"results/{log_name}",
            "equity_curve": meta["equity_curve"],
        }
        records.append(record)
        print(
            f"[{run_id}] {days} days, {total['trades']} trades "
            f"({total['wins']}W/{total['losses']}L), "
            f"net ${total['net_usd']:+.2f} / {total['net_r']:+.2f}R, "
            f"equity -> {total['ending_equity_usd']}, "
            f"maxDD ${total['max_drawdown_usd']}, "
            f"trough ${total['equity_trough_usd']} on {total['equity_trough_date']}, "
            f"H1 ${half1['net_usd']:+.2f}, H2 ${half2['net_usd']:+.2f}, "
            f"passed={record['passed']}"
        )

    winner = next((r for r in records if r["passed"]), None)
    out_doc = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "execution_model": (
            "Paper simulation only. Entries fill at the engulfing bar's "
            "close; exits at stop/target (V3/V5 partial leg at the +1.5R "
            "price); NO slippage, spread, or commissions modeled. The "
            "simulator never halts for balance or margin."
        ),
        "spec": {
            "starting_balance_usd": 500,
            "risk_budget_usd": 150,
            "futures_max_contracts": 5,
            "rr_target": 3.0,
            "one_trade_per_day": True,
            "entry_cutoff": "12:00 ET",
            "flatten_time": "15:55 ET",
            "window": "2024-10-21 -> 2026-10-20",
            "data": "GC.v.0 continuous, GLBX.MDP3 ohlcv-1m, traded as MGC ($10/pt)",
            "data_cost_usd": 2.56,
        },
        "pass_criteria": (
            "Checked in priority order V0 -> V3 -> V5; the first variant "
            "passing ALL of (a) 24-month net $ > 0 and net R > 0, "
            "(b) second half (2025-10-21..2026-10-20) net $ > 0 and "
            "net R > 0, (c) day-end equity trough above $0, is the "
            "bot's strategy. If none pass, no bot is built."
        ),
        "data_quality": data_quality,
        "runs": records,
        "winner": winner["id"] if winner else None,
        "outcome": (
            f"{winner['variant_label']} is the first variant passing all "
            f"three criteria; it becomes the gold bot's strategy."
            if winner else
            "No variant passed all three criteria; NO gold bot is built."
        ),
    }
    out = ROOT / "results" / "gold24.json"
    out.write_text(json.dumps(out_doc, indent=2))
    print(f"\ngold24.json written ({len(records)} runs). Outcome: {out_doc['outcome']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
