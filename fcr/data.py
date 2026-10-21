"""Data sources for the FCR backtester.

All loaders return a pandas DataFrame with columns:
    timestamp (tz-aware, America/New_York), open, high, low, close, volume
sorted ascending by timestamp.

Sources:
  * load_csv        - user-supplied 1-minute OHLCV CSV
  * fetch_yfinance  - Yahoo Finance via yfinance (only ~7 days of 1m data)
  * generate_synthetic - seeded random data with scripted morning impulses,
                         purely to smoke-test the pipeline (NOT market data)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TZ = "America/New_York"
_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    if "volume" not in df.columns:
        df["volume"] = 0.0
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(TZ)
    else:
        ts = ts.dt.tz_convert(TZ)
    df["timestamp"] = ts
    return df[_COLUMNS].sort_values("timestamp").reset_index(drop=True)


def load_csv(path) -> pd.DataFrame:
    """Load 1-minute OHLCV bars from a CSV file.

    Expected header (case-insensitive): a time column named one of
    timestamp/datetime/time/date, plus open, high, low, close (volume
    optional). Naive timestamps are interpreted as America/New_York.
    """
    df = pd.read_csv(path)
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    for candidate in ("timestamp", "datetime", "time", "date"):
        if candidate in df.columns:
            df = df.rename(columns={candidate: "timestamp"})
            break
    missing = {"timestamp", "open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing required columns: {sorted(missing)}")
    return _normalize(df)


def fetch_yfinance(symbol: str = "SPY", period: str = "7d", interval: str = "1m") -> pd.DataFrame:
    """Fetch 1-minute bars from Yahoo Finance.

    Yahoo only serves roughly the last 7 days of 1-minute data, so this is
    enough for a pipeline check, not a real backtest.
    """
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError(
            "yfinance is not installed in this Python environment. "
            "Install it (e.g. in the project venv: .venv/bin/pip install yfinance) "
            "or supply data with --csv."
        ) from exc
    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
    if df is None or len(df) == 0:
        raise RuntimeError(f"yfinance returned no data for {symbol} ({period}, {interval})")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.reset_index().rename(columns={"Datetime": "timestamp", "Date": "timestamp"})
    return _normalize(df)


def generate_synthetic(days: int = 15, seed: int = 7, start_price: float = 660.0) -> pd.DataFrame:
    """Generate synthetic 1-minute bars (seeded, reproducible).

    *** SYNTHETIC DATA — not real market data. ***
    Each day is a random walk around a slow drift. On most days a scripted
    directional impulse fires during the morning (a fast push over ~3 bars,
    then a partial retracement) so the break -> FVG -> retest chain has
    something to bite on. Good for smoke-testing the pipeline only; results
    on this data say nothing about the strategy's real-world performance.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end="2026-10-20", periods=days)
    frames = []
    price = start_price
    for day in dates:
        times = pd.date_range(
            start=day + pd.Timedelta(hours=9, minutes=30), periods=390, freq="1min", tz=TZ
        )
        # Slow intraday drift + per-minute noise on log price.
        drift = rng.normal(0, 0.00004, 390)
        noise = rng.normal(0, 0.00055, 390)
        log_path = np.cumsum(drift + noise)

        # Scripted morning impulse: fast push, then partial retracement,
        # then hold — the classic displacement/retest shape.
        impulse = np.zeros(390)
        if rng.random() < 0.75:
            t0 = int(rng.integers(8, 60))  # starts 9:38-10:29
            direction = 1.0 if rng.random() < 0.5 else -1.0
            size = direction * rng.uniform(0.0018, 0.0035)  # 0.18%-0.35% push
            for k in range(3):  # ramp over 3 bars
                impulse[t0 + k :] += size / 3.0
            hold = size * rng.uniform(0.35, 0.6)  # retrace to 35-60% of push
            decay = np.exp(-np.arange(390 - (t0 + 3)) / 12.0)
            impulse[t0 + 3 :] += (hold - size) * (1.0 - decay)
        closes = price * np.exp(log_path + impulse)

        opens = np.empty(390)
        opens[0] = price
        opens[1:] = closes[:-1]
        spread = np.abs(rng.normal(0, 0.03, 390)) + 0.01
        highs = np.maximum(opens, closes) + spread
        lows = np.minimum(opens, closes) - spread
        volumes = rng.integers(5_000, 60_000, 390).astype(float)

        frames.append(
            pd.DataFrame(
                {
                    "timestamp": times,
                    "open": opens,
                    "high": highs,
                    "low": lows,
                    "close": closes,
                    "volume": volumes,
                }
            )
        )
        price = float(closes[-1])
    return pd.concat(frames).reset_index(drop=True)
