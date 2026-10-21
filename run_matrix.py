#!/usr/bin/env python3
"""Run the FCR backtest matrix on free Yahoo Finance data (paper only).

Runs:
  * 60 days of 5-minute bars — MNQ futures + QQQ ("60-day functional test")
  * ~7 days of 1-minute bars  — MNQ futures + QQQ (the true 1m config)

Writes per-run trade logs to results/trades_<run_id>.csv and a combined
machine-readable results/summary.json consumed by build_site.py.

Free-data limits (be honest about them): Yahoo serves at most 60 days of
5-minute bars and ~7 days of 1-minute bars. Futures symbols are the
unadjusted continuous front-month series, so contract rolls appear as
price jumps. Run with the project venv:  .venv/bin/python run_matrix.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from fcr.backtest import TRADE_LOG_COLUMNS, run_backtest, run_stats
from fcr.config import load_config
from fcr.data import fetch_yfinance

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
EXPECTED_BARS_PER_DAY = {"5m": 78, "1m": 390}
LARGE_GAP_PCT = 1.25  # % close-to-open move worth flagging


def sanity_checks(df: pd.DataFrame, bar_label: str, futures: bool) -> list[str]:
    """Cheap data-quality checks; returns human-readable notes."""
    notes: list[str] = []
    session = df[
        (df["timestamp"].dt.time >= pd.Timestamp("09:30").time())
        & (df["timestamp"].dt.time < pd.Timestamp("16:00").time())
    ].copy()
    if session.empty:
        return ["No RTH (09:30-16:00 ET) bars at all — data unusable."]
    session["_date"] = session["timestamp"].dt.date
    counts = session.groupby("_date").size()
    expected = EXPECTED_BARS_PER_DAY[bar_label]
    thin = counts[counts < 0.9 * expected]
    if len(thin):
        notes.append(
            f"{len(thin)} RTH day(s) have <90% of the expected {expected} bars "
            f"(thinnest: {int(counts.min())} bars on {counts.idxmin()})."
        )
    # Missing weekdays inside the covered range
    days = pd.DatetimeIndex(sorted(counts.index))
    bdays = pd.bdate_range(days[0], days[-1])
    missing = [d.date().isoformat() for d in bdays if d.date() not in set(counts.index)]
    if missing:
        notes.append(f"{len(missing)} weekday(s) with no RTH bars: {', '.join(missing[:6])}"
                     + ("…" if len(missing) > 6 else ""))
    if futures:
        # Close-to-next-open moves span the ~17.5h globex session, so only
        # very large ones are worth flagging (mostly real news moves).
        gaps = []
        dates = sorted(counts.index)
        for prev, cur in zip(dates, dates[1:]):
            prev_close = session[session["_date"] == prev]["close"].iloc[-1]
            cur_open = session[session["_date"] == cur]["open"].iloc[0]
            gaps.append((cur.isoformat(), float(cur_open - prev_close),
                         float((cur_open - prev_close) / prev_close * 100)))
        big = [(d, g, p) for d, g, p in gaps if abs(p) > 1.25]
        if big:
            worst = max(big, key=lambda x: abs(x[2]))
            notes.append(
                f"{len(big)} close-to-open move(s) > 1.25% across the globex "
                f"session (largest {worst[2]:+.2f}% / {worst[1]:+.1f} pts into "
                f"{worst[0]}) — mostly real market moves over the ~17.5h window."
            )
        notes.append(
            "Continuous front-month series is UNADJUSTED: quarterly roll "
            "splices (e.g. the 2026-09-18 MNQ roll) appear as price jumps "
            "between days; the strategy only uses intraday levels, so splices "
            "do not create or erase trades, but cross-day comparisons are tainted."
        )
    if len(thin):
        last_day = counts.index.max()
        if counts.idxmin() == last_day:
            notes.append(
                f"The thinnest day ({counts.idxmin()}, {int(counts.min())} bars) is "
                "the current session — partial data at fetch time; treat that "
                "day's non-result as no information."
            )
    return notes


def fetch_with_fallback(symbols: list[str], period: str, interval: str, cache_name: str):
    """Try each symbol in order; cache the winner's bars under data/."""
    errors = []
    for sym in symbols:
        try:
            df = fetch_yfinance(sym, period=period, interval=interval)
            cache = ROOT / "data" / cache_name
            cache.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(cache, index=False)
            return sym, df
        except Exception as exc:  # yfinance raises a variety of errors
            errors.append(f"{sym}: {exc}")
    raise RuntimeError("All fetches failed — " + " | ".join(errors))


def do_run(run_id: str, cfg: dict, data_symbol: str, df: pd.DataFrame,
           bar_label: str, futures: bool) -> dict:
    rows, days, meta = run_backtest(df, cfg)
    stats = run_stats(rows, days, meta)
    log_name = f"trades_{run_id}.csv"
    out = ROOT / "results" / log_name
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=TRADE_LOG_COLUMNS).to_csv(out, index=False)
    record = {
        "id": run_id,
        "symbol": cfg["symbol"],
        "data_symbol": data_symbol,
        "bar": bar_label,
        "instrument_type": cfg["instrument_type"],
        "point_value_usd": cfg["point_value_usd"],
        "date_range": {
            "start": df["timestamp"].min().date().isoformat(),
            "end": df["timestamp"].max().date().isoformat(),
        },
        "bars_loaded": int(len(df)),
        "trade_log": f"results/{log_name}",
        **stats,
    }
    print(
        f"[{run_id}] {data_symbol} {bar_label}: {days} days, {stats['trades']} trades, "
        f"win rate {stats['win_rate']}, net ${stats['net_usd']:.2f} / {stats['net_r']:+.2f}R, "
        f"equity -> {stats['ending_equity_usd']}, maxDD ${stats['max_drawdown_usd']}, "
        f"skipped-for-size {stats['skipped_for_size']}"
    )
    return record


def main() -> int:
    base_cfg = load_config(ROOT / "config.yaml")

    mnq_cfg = dict(base_cfg)  # MNQ futures config straight from config.yaml
    qqq_cfg = dict(base_cfg)
    qqq_cfg.update(
        symbol="QQQ",
        instrument_type="equity",
        point_value_usd=1.0,
        max_units=None,
    )

    runs: list[dict] = []
    notes: list[str] = []

    # --- 60 days of 5-minute bars --------------------------------------
    sym, df5 = fetch_with_fallback(["MNQ=F", "NQ=F"], "60d", "5m", "mnq_5m_60d.csv")
    fut_cfg = mnq_cfg
    if sym == "NQ=F":
        fut_cfg = dict(mnq_cfg)
        fut_cfg.update(symbol="NQ", point_value_usd=20.0)
        notes.append("MNQ=F returned nothing; the futures runs use NQ=F "
                     "($20/pt full-size E-mini) with the same $150 budget / 5-cap.")
    notes += [f"MNQ 5m: {n}" for n in sanity_checks(df5, "5m", futures=True)]
    runs.append(do_run("mnq_5m_60d", fut_cfg, sym, df5, "5m", futures=True))

    sym_q5, dfq5 = fetch_with_fallback(["QQQ"], "60d", "5m", "qqq_5m_60d.csv")
    notes += [f"QQQ 5m: {n}" for n in sanity_checks(dfq5, "5m", futures=False)]
    runs.append(do_run("qqq_5m_60d", qqq_cfg, sym_q5, dfq5, "5m", futures=False))

    # --- ~7 days of 1-minute bars --------------------------------------
    sym1, df1 = fetch_with_fallback(["MNQ=F", "NQ=F"], "7d", "1m", "mnq_1m_7d.csv")
    fut1_cfg = mnq_cfg if sym1 == "MNQ=F" else fut_cfg
    notes += [f"MNQ 1m: {n}" for n in sanity_checks(df1, "1m", futures=True)]
    runs.append(do_run("mnq_1m_7d", fut1_cfg, sym1, df1, "1m", futures=True))

    sym_q1, dfq1 = fetch_with_fallback(["QQQ"], "7d", "1m", "qqq_1m_7d.csv")
    notes += [f"QQQ 1m: {n}" for n in sanity_checks(dfq1, "1m", futures=False)]
    runs.append(do_run("qqq_1m_7d", qqq_cfg, sym_q1, dfq1, "1m", futures=False))

    # The simulator never halts for balance or margin. If a run's equity
    # went negative (or drawdown exceeded the starting balance), say so —
    # a live $500 account could not have taken every one of those trades.
    for r in runs:
        if r["ending_equity_usd"] is not None and r["ending_equity_usd"] < 0:
            notes.append(
                f"{r['id']}: ending equity is negative (${r['ending_equity_usd']:.2f}) "
                "because the paper sim never stops for balance/margin. A live $500 "
                "account would have been margin-stopped well before this — read "
                "the run as strategy behavior, not as an achievable account history."
            )

    summary = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "execution_model": "Paper simulation only. Entries fill at the engulfing "
                           "bar's close; exits at stop/target; NO slippage, spread, "
                           "or commissions modeled.",
        "config": {
            "instrument": base_cfg["symbol"],
            "point_value_usd": base_cfg["point_value_usd"],
            "risk_budget_usd": base_cfg["risk_budget_usd"],
            "max_contracts": base_cfg["max_units"],
            "starting_balance_usd": base_cfg["starting_balance_usd"],
            "rr_target": base_cfg["rr_target"],
            "first_candle": "09:30-09:35 ET",
            "entry_cutoff": "12:00 ET",
            "flatten_time": "15:55 ET",
            "one_trade_per_day": True,
        },
        "runs": runs,
        "data_notes": notes,
    }
    out = ROOT / "results" / "summary.json"
    out.write_text(json.dumps(summary, indent=2))
    print(f"\nSummary written to {out}")
    for n in notes:
        print(f"NOTE: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
