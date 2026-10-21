"""Day-by-day backtester for the First Candle Rule strategy (paper only).

Runs the shared FCREngine over historical 1-minute bars, one fresh engine
per trading day, and writes a trade log CSV plus a printed summary.
"""
from __future__ import annotations

import argparse
from datetime import time
from pathlib import Path

import pandas as pd

from .config import load_config
from .data import fetch_yfinance, generate_synthetic, load_csv
from .engine import FCREngine
from .patterns import Bar

SESSION_START = time(9, 30)
SESSION_END = time(16, 0)

TRADE_LOG_COLUMNS = [
    "date",
    "direction",
    "entry_time",
    "entry_price",
    "stop",
    "target",
    "exit_time",
    "exit_price",
    "exit_reason",
    "contracts",
    "pnl_usd",
    "r_multiple",
]


def max_drawdown_usd(equity_curve: list[dict]) -> float:
    """Largest peak-to-trough equity decline, in dollars."""
    peak = None
    worst = 0.0
    for point in equity_curve:
        eq = point["equity"]
        peak = eq if peak is None else max(peak, eq)
        worst = max(worst, peak - eq)
    return round(worst, 2)


def run_backtest(
    df: pd.DataFrame, cfg: dict, starting_balance: float | None = None
) -> tuple[list[dict], int, dict]:
    """Run the engine over all days in df.

    Returns (trade rows, trading days run, meta) where meta carries the
    skipped-for-size count and, when a starting balance is known, the
    day-by-day equity curve, ending equity and max drawdown.
    """
    if starting_balance is None:
        starting_balance = cfg.get("starting_balance_usd")
    rows: list[dict] = []
    days = 0
    skipped = 0
    equity = float(starting_balance) if starting_balance is not None else 0.0
    equity_curve: list[dict] = []
    if starting_balance is not None:
        equity_curve.append({"date": "start", "equity": round(equity, 2)})
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
        engine = FCREngine(cfg)
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
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
            )
            engine.on_bar(last_bar)
        # Data ended mid-trade (partial day): close at the last bar's close.
        if engine.position is not None and last_bar is not None:
            engine.force_close(last_bar.ts, last_bar.close, reason="DATA_END")
        skipped += engine.skipped_for_size
        equity += sum(t.pnl_usd for t in engine.trades)
        rows.extend(t.as_row() for t in engine.trades)
        if starting_balance is not None:
            equity_curve.append({"date": date.isoformat(), "equity": round(equity, 2)})
    meta = {
        "skipped_for_size": skipped,
        "starting_balance_usd": starting_balance,
        "ending_equity_usd": round(equity, 2) if starting_balance is not None else None,
        "max_drawdown_usd": max_drawdown_usd(equity_curve) if equity_curve else None,
        "equity_curve": equity_curve,
    }
    return rows, days, meta


def run_stats(rows: list[dict], days: int, meta: dict) -> dict:
    """Aggregate one backtest run into the summary.json record shape."""
    n = len(rows)
    pnls = [r["pnl_usd"] for r in rows]
    rs = [r["r_multiple"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "trading_days": days,
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4) if n else None,
        "net_usd": round(sum(pnls), 2),
        "net_r": round(sum(rs), 3),
        "starting_balance_usd": meta["starting_balance_usd"],
        "ending_equity_usd": meta["ending_equity_usd"],
        "max_drawdown_usd": meta["max_drawdown_usd"],
        "avg_contracts": round(sum(r["contracts"] for r in rows) / n, 2) if n else None,
        "skipped_for_size": meta["skipped_for_size"],
        "equity_curve": meta["equity_curve"],
    }


def summarize(rows: list[dict], days: int, meta: dict | None = None) -> str:
    n = len(rows)
    extra = ""
    if meta:
        extra = f"\nSkipped for size: {meta['skipped_for_size']}"
        if meta["ending_equity_usd"] is not None:
            extra += (
                f"\nEquity: ${meta['starting_balance_usd']:,.2f} -> "
                f"${meta['ending_equity_usd']:,.2f} "
                f"(max drawdown ${meta['max_drawdown_usd']:,.2f})"
            )
    if n == 0:
        return f"Days run: {days}\nTrades: 0 (the FCR pattern never completed){extra}"
    pnls = [r["pnl_usd"] for r in rows]
    rs = [r["r_multiple"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    lines = [
        f"Days run: {days}",
        f"Trades: {n}",
        f"Win rate: {wins / n:.1%} ({wins}W / {n - wins}L)",
        f"Net P&L: ${sum(pnls):,.2f}",
        f"Net R: {sum(rs):+.2f}R",
        f"Avg R per trade: {sum(rs) / n:+.2f}R",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="FCR paper backtester (no live trading)")
    parser.add_argument("--config", default=str(root / "config.yaml"))
    parser.add_argument("--csv", help="Path to a 1-minute OHLCV CSV file")
    parser.add_argument("--fetch", action="store_true", help="Fetch ~7d of 1m data via yfinance")
    parser.add_argument("--synthetic", action="store_true", help="Use generated synthetic data (smoke test)")
    parser.add_argument("--days", type=int, default=15, help="Synthetic days to generate")
    parser.add_argument("--seed", type=int, default=7, help="Synthetic data seed")
    parser.add_argument("--out", default=str(root / "results" / "trade_log.csv"))
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    if args.csv:
        source = f"CSV file: {args.csv}"
        df = load_csv(args.csv)
    elif args.fetch:
        source = f"yfinance fetch: {cfg['symbol']} 1m, last ~7 days (all Yahoo offers)"
        df = fetch_yfinance(cfg["symbol"])
    else:
        source = (
            f"SYNTHETIC data ({args.days} days, seed {args.seed}) — smoke test only, "
            "not real market data"
        )
        df = generate_synthetic(days=args.days, seed=args.seed)
        synth_path = root / "data" / "synthetic_spy_1m.csv"
        df.to_csv(synth_path, index=False)
        print(f"Wrote synthetic data to {synth_path}")

    print(f"Data source: {source}")
    print(f"Bars loaded: {len(df):,}")

    rows, days, meta = run_backtest(df, cfg)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=TRADE_LOG_COLUMNS).to_csv(out, index=False)
    print(f"Trade log written to {out}")
    print("--- Summary ---")
    print(summarize(rows, days, meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
