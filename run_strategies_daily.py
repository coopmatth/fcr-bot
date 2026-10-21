#!/usr/bin/env python3
"""Daily-strategy backtests (paper only): Donchian trend + Connors RSI(2).

Companion to the intraday FCR work: two classic DAILY-bar systems, run
under the same house spec (see fcr/daily.py for the full definitions):

  * donchian_trend  - 20-day channel breakout, 2xATR(14) stop, 10-day
                      opposite-channel exit; SPY, QQQ, and gold
                      (GC=F tape traded as MGC, $10/pt, cap 5 contracts)
  * rsi2_reversion  - Connors: close > SMA(200) and RSI(2) < 5 -> buy,
                      exit at the first close > SMA(5); no stop; SPY
                      and QQQ only

Data (data/, fetched by fetch_daily_data.py; raw CSVs are NOT in the
repo): Yahoo raw daily OHLC, 500 calendar days ending 2026-10-20. Only
the last 120 TRADING days of each symbol are traded
(2026-04-30 -> 2026-10-20); the earlier ~224 bars are indicator warmup
(the RSI(2) system needs its 200-day SMA warm at the window start).
GC=F is a continuous front-month splice — levels jump slightly at
contract rolls; acceptable for a daily test, flagged here.

Common spec: $500 starting paper equity tracked across the run (the
simulator never halts on a negative balance; stats flag it), $150 risk
budget per trade, one position at a time per strategy per symbol. NO
slippage/spread/commissions in the gross numbers; every run also
reports a net-of-friction twin at a fixed round-trip assumption
(equities $0.04/share; MGC $4.40/contract).

Outputs: results/trades_<run_id>.csv per run and one JSON summary
(default results/daily_strategies.json).

Run everything:      .venv/bin/python run_strategies_daily.py
Smoke (SPY only):    .venv/bin/python run_strategies_daily.py \
                         --symbols SPY --out-json results/daily_strategies_smoke.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from fcr.daily import (DAILY_TRADE_COLUMNS, daily_stats, load_daily_csv,
                       run_donchian, run_rsi2)

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

# symbol -> (data file, traded-as label, point value, max units,
#            friction $ per unit per round trip)
SYMBOLS = {
    "SPY": ("spy_daily.csv", "SPY shares", 1.0, None, 0.04),
    "QQQ": ("qqq_daily.csv", "QQQ shares", 1.0, None, 0.04),
    "GC": ("gc_daily.csv", "MGC contracts (GC=F tape)", 10.0, 5, 4.40),
}
DONCHIAN_SYMBOLS = ["SPY", "QQQ", "GC"]
RSI2_SYMBOLS = ["SPY", "QQQ"]


def run_one(strategy: str, symbol: str, window_days: int) -> tuple[dict, list[dict]]:
    data_file, traded_as, pv, max_units, friction = SYMBOLS[symbol]
    bars = load_daily_csv(ROOT / "data" / data_file)
    start_idx = len(bars) - window_days
    if strategy == "donchian":
        rows, meta = run_donchian(
            bars, symbol, point_value=pv, max_units=max_units,
            friction_per_unit=friction, window_start_idx=start_idx)
        strategy_name = "donchian_trend"
    else:
        rows, meta = run_rsi2(
            bars, symbol, friction_per_unit=friction,
            window_start_idx=start_idx)
        strategy_name = "rsi2_reversion"
    run_id = f"{strategy}_{symbol.lower()}_daily"
    stats = daily_stats(rows)
    record = {
        "id": run_id,
        "strategy": strategy_name,
        "symbol": symbol,
        "traded_as": traded_as,
        "point_value_usd": pv,
        "max_units": max_units,
        "friction_per_unit_round_trip_usd": friction,
        "data_file": f"data/{data_file}",
        "window": {
            "start": bars["date"].iloc[start_idx].strftime("%Y-%m-%d"),
            "end": bars["date"].iloc[-1].strftime("%Y-%m-%d"),
            "trading_days": window_days,
            "warmup_bars": start_idx,
        },
        "skipped_for_size": meta["skipped_for_size"],
        "trade_log": f"results/trades_{run_id}.csv",
        **stats,
    }
    return record, rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategies", default="donchian,rsi2")
    ap.add_argument("--symbols", default="SPY,QQQ,GC")
    ap.add_argument("--window-days", type=int, default=120)
    ap.add_argument("--out-json", default="results/daily_strategies.json")
    args = ap.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    plan = []
    if "donchian" in strategies:
        plan += [("donchian", s) for s in DONCHIAN_SYMBOLS if s in symbols]
    if "rsi2" in strategies:
        plan += [("rsi2", s) for s in RSI2_SYMBOLS if s in symbols]

    records = []
    for strategy, symbol in plan:
        record, rows = run_one(strategy, symbol, args.window_days)
        out = ROOT / record["trade_log"]
        pd.DataFrame(rows, columns=DAILY_TRADE_COLUMNS).to_csv(out, index=False)
        records.append(record)
        net = record["net_of_friction"]
        print(f"{record['id']}: {record['trades']} trades "
              f"({record['wins']}W/{record['losses']}L, "
              f"WR {record['win_rate']}), net ${record['net_usd']:+.2f} "
              f"/ {record['net_r']:+.2f}R, equity $500 -> "
              f"${record['ending_equity_usd']:.2f}, maxDD "
              f"${record['max_drawdown_usd']:.2f}, avg hold "
              f"{record['avg_hold_days']}d | net-of-friction "
              f"${net['net_usd']:+.2f} / {net['net_r']:+.2f}R")

    doc = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "execution_model": (
            "Paper simulation on daily bars. Donchian: signals at day t "
            "close fill at t+1 open; stops checked before channel exits; "
            "gap-through-stop fills at the bar open; channel exits fill "
            "at the next open. RSI(2): entries fill AT the signal "
            "day's close (canonical Connors); exits at the first "
            "close > SMA(5), filled at that "
            "close; NO stop. Gross numbers model no slippage/spread/"
            "commissions; net_of_friction twins deduct a fixed "
            "round-trip charge per unit. The simulator never halts for "
            "balance or margin — equity_went_negative flags a run whose "
            "paper equity dipped below $0."),
        "spec": {
            "starting_balance_usd": 500,
            "risk_budget_usd": 150,
            "window_days": args.window_days,
            "one_position_at_a_time": True,
            "donchian": "20d channel entry, stop = entry -/+ 2xATR(14) "
                        "(ATR at signal day), units = floor(150 / "
                        "(2 x ATR x point value)), 10d opposite-channel "
                        "close -> exit next open",
            "rsi2": "close > SMA(200) and RSI(2) < 5 -> buy at the "
                    "signal close (canonical Connors); "
                    "shares = floor(3000 / signal close) ($3,000 notional "
                    "= $150 budget vs a 5% adverse move); exit at first "
                    "close > SMA(5); R = $150 proxy for every trade",
            "friction_round_trip": "SPY/QQQ $0.04/share; MGC $4.40/contract",
            "data": "Yahoo raw daily OHLC via yfinance (fetch_daily_data.py); "
                    "GC=F is a continuous front-month splice",
        },
        "runs": records,
    }
    out_json = ROOT / args.out_json
    out_json.write_text(json.dumps(doc, indent=1))
    print(f"\nWrote {out_json} ({len(records)} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
