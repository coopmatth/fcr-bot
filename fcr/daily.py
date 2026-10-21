"""Daily-bar strategies (paper only): Donchian trend + Connors RSI(2).

Deliberately separate from the intraday FCR engine (fcr/engine.py): these
strategies think in days, not minutes, and share none of its session
machinery. Data comes from fetch_daily_data.py (Yahoo raw daily OHLC).

Shared conventions with the rest of the project:
  * paper equity starts at $500 and is tracked across the run; the
    simulator NEVER halts on a negative balance (stats flag it instead)
  * risk budget $150 per trade; one position at a time per strategy
  * every run is reported gross AND net of a fixed friction assumption
    per round trip (equities $0.04/share; gold-as-MGC $4.40/contract)
  * only the last WINDOW_DAYS trading days are traded; earlier bars in
    the file are indicator warmup only

donchian_trend (Turtle-style), SPY/QQQ/GC:
  Signal at day t close: close > highest high of the prior 20 days ->
  long; close < lowest low of the prior 20 days -> short. Enter at the
  t+1 open. Initial stop = entry -/+ 2 x ATR(14) with the ATR taken at
  the signal day. Units = floor(150 / (2 x ATR x point_value)), capped
  at 5 for futures (GC traded as MGC, $10/pt), uncapped for shares.
  Exit: a close beyond the opposite 10-day channel (longs: close <
  lowest low of prior 10 days) exits at the NEXT day's open. The stop
  is checked first each day: a bar that gaps through the stop exits at
  that bar's open; a bar whose range touches the stop exits at the stop
  price. R = initial risk $ = units x 2 x ATR x point_value.

rsi2_reversion (Connors), SPY/QQQ only:
  Signal at day t close: close > SMA(200) AND RSI(2) < 5 -> buy AT
  that same close (canonical Connors: the entry fills at the signal
  day's close, NOT the next open). There is NO stop. Size is a fixed
  $3,000 notional proxy: shares = floor(3000 / signal-day close) —
  i.e. the $150 risk budget against a 5% adverse move; R is therefore
  the flat $150 proxy for every trade and R-multiples are pnl / 150.
  Exit AT the close of the first day (entry day included) whose
  close > SMA(5).

A position still open when the data ends is marked to market at the
final close with exit_reason "end_of_data". After an exit at a day's
close (RSI(2)) or open (Donchian), a new signal may be taken at that
same day's close — never two positions at once.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RISK_BUDGET_USD = 150.0
WINDOW_DAYS = 120

DAILY_TRADE_COLUMNS = [
    "strategy", "symbol", "direction", "entry_date", "exit_date",
    "entry_price", "exit_price", "exit_reason", "units", "hold_days",
    "risk_usd", "pnl_usd", "pnl_net_usd", "r_multiple", "r_net",
]


def load_daily_csv(path) -> pd.DataFrame:
    """Load a daily OHLCV CSV (date,open,high,low,close,volume)."""
    df = pd.read_csv(path)
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    missing = {"date", "open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"daily CSV is missing columns: {sorted(missing)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    cols = ["date", "open", "high", "low", "close", "volume"]
    return df[cols].sort_values("date").reset_index(drop=True)


# ----------------------------------------------------------------------
# Indicators (Wilder smoothing, matching the classic definitions)
# ----------------------------------------------------------------------

def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average; NaN until `period` values exist."""
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) >= period:
        csum = np.cumsum(np.insert(values, 0, 0.0))
        out[period - 1:] = (csum[period:] - csum[:-period]) / period
    return out


def wilder_atr(high, low, close, period: int = 14) -> np.ndarray:
    """Wilder ATR. Seeded at index `period` as the simple mean of
    TR[1..period]; then ATR_t = (ATR_{t-1} x (p-1) + TR_t) / p."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    if n > period:
        atr[period] = tr[1:period + 1].mean()
        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def wilder_rsi(close, period: int = 2) -> np.ndarray:
    """Wilder RSI. The first average gain/loss (at index `period`) is
    the simple mean of the first `period` changes; subsequent values use
    Wilder smoothing. avg_loss == 0 -> RSI 100."""
    close = np.asarray(close, dtype=float)
    n = len(close)
    rsi = np.full(n, np.nan)
    if n <= period:
        return rsi
    delta = np.diff(close)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    def _value(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    rsi[period] = _value(avg_gain, avg_loss)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i + 1] = _value(avg_gain, avg_loss)
    return rsi


def prior_channel(values_high, values_low, period: int):
    """(prior_high, prior_low): index t uses bars [t-period, t) only —
    the current bar never contributes to its own channel."""
    ph = pd.Series(values_high, dtype=float).rolling(period).max().shift(1)
    pl = pd.Series(values_low, dtype=float).rolling(period).min().shift(1)
    return ph.to_numpy(), pl.to_numpy()


# ----------------------------------------------------------------------
# Trade bookkeeping
# ----------------------------------------------------------------------

def _record(rows, strategy, symbol, pos, exit_idx, exit_price, reason,
            dates, point_value, friction_per_unit):
    entry_price = pos["entry_price"]
    units = pos["units"]
    sign = 1.0 if pos["direction"] == "long" else -1.0
    pnl = sign * (exit_price - entry_price) * units * point_value
    friction = friction_per_unit * units
    pnl_net = pnl - friction
    risk = pos["risk_usd"]
    rows.append({
        "strategy": strategy,
        "symbol": symbol,
        "direction": pos["direction"],
        "entry_date": dates[pos["entry_idx"]],
        "exit_date": dates[exit_idx],
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "exit_reason": reason,
        "units": units,
        "hold_days": exit_idx - pos["entry_idx"],
        "risk_usd": round(risk, 2),
        "pnl_usd": round(pnl, 2),
        "pnl_net_usd": round(pnl_net, 2),
        "r_multiple": round(pnl / risk, 3) if risk else 0.0,
        "r_net": round(pnl_net / risk, 3) if risk else 0.0,
    })


# ----------------------------------------------------------------------
# Strategy 1: Donchian trend
# ----------------------------------------------------------------------

def run_donchian(bars: pd.DataFrame, symbol: str, *, point_value: float,
                 max_units: int | None, friction_per_unit: float,
                 window_start_idx: int, entry_n: int = 20, exit_n: int = 10,
                 atr_period: int = 14,
                 risk_budget: float = RISK_BUDGET_USD) -> tuple[list[dict], dict]:
    o = bars["open"].to_numpy(float)
    h = bars["high"].to_numpy(float)
    lo = bars["low"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    dates = bars["date"].dt.strftime("%Y-%m-%d").tolist()
    n = len(bars)
    entry_high, entry_low = prior_channel(h, lo, entry_n)
    exit_high, exit_low = prior_channel(h, lo, exit_n)
    atr = wilder_atr(h, lo, c, atr_period)

    rows: list[dict] = []
    pos = None
    pending_entry = None
    skipped = 0

    for t in range(n):
        # 1) Fill a pending entry at today's open.
        if pending_entry is not None and pos is None:
            direction, atr_t = pending_entry
            units = int(risk_budget // (2.0 * atr_t * point_value))
            if max_units is not None:
                units = min(units, max_units)
            if units <= 0:
                skipped += 1
            else:
                entry_price = o[t]
                stop = (entry_price - 2.0 * atr_t if direction == "long"
                        else entry_price + 2.0 * atr_t)
                pos = {"direction": direction, "entry_idx": t,
                       "entry_price": entry_price, "stop": stop,
                       "units": units,
                       "risk_usd": units * 2.0 * atr_t * point_value,
                       "pending_exit": False}
            pending_entry = None

        # 2) A pending channel exit fills at today's open.
        if pos is not None and pos["pending_exit"]:
            _record(rows, "donchian_trend", symbol, pos, t, o[t],
                    "channel_exit", dates, point_value, friction_per_unit)
            pos = None

        # 3) Stop check for the day (entry day included).
        if pos is not None:
            stop = pos["stop"]
            exit_price = reason = None
            if pos["direction"] == "long":
                if o[t] <= stop:
                    exit_price, reason = o[t], "stop_gap"
                elif lo[t] <= stop:
                    exit_price, reason = stop, "stop"
            else:
                if o[t] >= stop:
                    exit_price, reason = o[t], "stop_gap"
                elif h[t] >= stop:
                    exit_price, reason = stop, "stop"
            if reason is not None:
                _record(rows, "donchian_trend", symbol, pos, t, exit_price,
                        reason, dates, point_value, friction_per_unit)
                pos = None

        # 4) Close of day: channel-exit decision for a held position,
        #    or a fresh signal when flat.
        if pos is not None:
            if pos["direction"] == "long" and c[t] < exit_low[t]:
                pos["pending_exit"] = True
            elif pos["direction"] == "short" and c[t] > exit_high[t]:
                pos["pending_exit"] = True
        elif (pending_entry is None and t >= window_start_idx
              and t + 1 < n and not np.isnan(atr[t])
              and not np.isnan(entry_high[t])):
            if c[t] > entry_high[t]:
                pending_entry = ("long", atr[t])
            elif c[t] < entry_low[t]:
                pending_entry = ("short", atr[t])

    if pos is not None:
        _record(rows, "donchian_trend", symbol, pos, n - 1, c[n - 1],
                "end_of_data", dates, point_value, friction_per_unit)
    return rows, {"skipped_for_size": skipped}


# ----------------------------------------------------------------------
# Strategy 2: Connors RSI(2) reversion (long-only, equities)
# ----------------------------------------------------------------------

def run_rsi2(bars: pd.DataFrame, symbol: str, *, friction_per_unit: float,
             window_start_idx: int, rsi_period: int = 2,
             rsi_threshold: float = 5.0, trend_period: int = 200,
             exit_period: int = 5, notional: float = 3000.0,
             risk_proxy: float = RISK_BUDGET_USD) -> tuple[list[dict], dict]:
    o = bars["open"].to_numpy(float)
    c = bars["close"].to_numpy(float)
    dates = bars["date"].dt.strftime("%Y-%m-%d").tolist()
    n = len(bars)
    trend = sma(c, trend_period)
    exit_sma = sma(c, exit_period)
    rsi = wilder_rsi(c, rsi_period)

    rows: list[dict] = []
    pos = None
    skipped = 0

    def _exit_check(t: int) -> None:
        """Exit at today's close if it clears SMA(exit_period)."""
        nonlocal pos
        if pos is not None and not np.isnan(exit_sma[t]) \
                and c[t] > exit_sma[t]:
            _record(rows, "rsi2_reversion", symbol, pos, t, c[t],
                    "sma5_exit", dates, 1.0, friction_per_unit)
            pos = None

    for t in range(n):
        # 1) Exit at the close of the first day close > SMA(exit_period)
        #    for a position entered on an earlier day. No stop.
        _exit_check(t)

        # 2) Fresh signal at today's close when flat: enter AT the
        #    close (canonical Connors fills at the signal close, not
        #    the next open), sized off that same close. The entry day
        #    itself can satisfy the exit rule, so re-check immediately.
        if pos is None and t >= window_start_idx \
                and t + 1 < n and not np.isnan(trend[t]) \
                and not np.isnan(rsi[t]):
            if c[t] > trend[t] and rsi[t] < rsi_threshold:
                units = int(notional // c[t])
                if units <= 0:
                    skipped += 1
                else:
                    pos = {"direction": "long", "entry_idx": t,
                           "entry_price": c[t], "units": units,
                           "risk_usd": risk_proxy}
                    _exit_check(t)

    if pos is not None:
        _record(rows, "rsi2_reversion", symbol, pos, n - 1, c[n - 1],
                "end_of_data", dates, 1.0, friction_per_unit)
    return rows, {"skipped_for_size": skipped}


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------

def _curve_stats(rows, pnl_key, r_key, starting_balance):
    equity = starting_balance
    peak = starting_balance
    min_equity = starting_balance
    max_dd = 0.0
    for row in rows:
        equity += row[pnl_key]
        peak = max(peak, equity)
        min_equity = min(min_equity, equity)
        max_dd = max(max_dd, peak - equity)
    wins = sum(1 for row in rows if row[pnl_key] > 0)
    n = len(rows)
    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4) if n else None,
        "net_usd": round(sum(r[pnl_key] for r in rows), 2),
        "net_r": round(sum(r[r_key] for r in rows), 3),
        "ending_equity_usd": round(equity, 2),
        "max_drawdown_usd": round(max_dd, 2),
    }


def daily_stats(rows: list[dict], starting_balance: float = 500.0) -> dict:
    """Headline stats on gross pnl, plus net-of-friction twins and the
    negative-equity flag (the simulator never halts on a busted paper
    account, so the flag is how a busted run is reported)."""
    stats = _curve_stats(rows, "pnl_usd", "r_multiple", starting_balance)
    net = _curve_stats(rows, "pnl_net_usd", "r_net", starting_balance)
    n = len(rows)
    # Track the gross equity trough for the flag/minimum.
    equity = starting_balance
    min_equity = starting_balance
    for row in rows:
        equity += row["pnl_usd"]
        min_equity = min(min_equity, equity)
    stats.update({
        "starting_balance_usd": starting_balance,
        "avg_hold_days": round(sum(r["hold_days"] for r in rows) / n, 2)
        if n else None,
        "avg_units": round(sum(r["units"] for r in rows) / n, 2)
        if n else None,
        "min_equity_usd": round(min_equity, 2),
        "equity_went_negative": bool(min_equity < 0),
        "net_of_friction": net,
    })
    return stats
