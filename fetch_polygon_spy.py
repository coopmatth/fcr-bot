#!/usr/bin/env python3
"""Polygon.io SPY 1-minute bars -> data/spy_1m_polygon.csv.

The arbiter data pull for the SPY validation: Databento's DBEQ.BASIC
SPY feed proved non-reproducible across pulls (see
results/validation24.json "diagnosis"), so the SPY winners are
re-checked on Polygon's consolidated (SIP) tape.

SOURCE
    Polygon.io (now Massive) aggregates API, via the connected
    custom.polygon connector — the API key lives in the credential
    vault and is attached by the helper as the `apiKey` query
    parameter; no key appears in this project:

        python3 ~/workspace/custom-connectors/custom.polygon/bin/poly.py \
            aggs --ticker SPY --from 2024-10-21 --to 2026-10-20 \
            --out data/spy_1m_polygon.jsonl

    adjusted=false (raw prices — the same unadjusted convention as
    the Databento files), sort ascending, 50,000-bar pages followed
    through `next_url`. The connector CLI self-paces to the Stocks
    Basic plan limit of 5 calls/minute. 2026-10-21 pull: 9 pages,
    431,824 bars.

CONVERSION
    Each JSONL line is one bar {"t": ms epoch UTC (bar start),
    "o","h","l","c","v","vw","n"}. The frame is reshaped to exactly
    what fetch_databento.bars_to_loader_csv consumes (UTC `ts_event`
    index + open/high/low/close/volume) and written through that
    same function, so the output CSV
    (timestamp,open,high,low,close,volume with tz-aware
    America/New_York timestamps) is byte-format-identical to the
    Databento files. ALL bars are kept, extended hours included —
    same as the Databento files; the strategy engines filter to
    their sessions themselves.

RUN
    .venv/bin/python fetch_polygon_spy.py [--pull]
        [--jsonl data/spy_1m_polygon.jsonl]
        [--out data/spy_1m_polygon.csv]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
POLY_CLI = (Path.home() / "workspace" / "custom-connectors"
            / "custom.polygon" / "bin" / "poly.py")
DEFAULT_JSONL = ROOT / "data" / "spy_1m_polygon.jsonl"
DEFAULT_OUT = ROOT / "data" / "spy_1m_polygon.csv"
PULL_FROM, PULL_TO = "2024-10-21", "2026-10-20"


def load_polygon_jsonl(path) -> pd.DataFrame:
    """JSONL bars -> Databento to_df() shape (UTC ts_event index)."""
    records = []
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        raise ValueError(f"no bars in {path}")
    raw = pd.DataFrame(records)
    ts = pd.to_datetime(raw["t"].astype("int64"), unit="ms", utc=True)
    df = pd.DataFrame(
        {
            "open": pd.to_numeric(raw["o"], errors="coerce").to_numpy(),
            "high": pd.to_numeric(raw["h"], errors="coerce").to_numpy(),
            "low": pd.to_numeric(raw["l"], errors="coerce").to_numpy(),
            "close": pd.to_numeric(raw["c"], errors="coerce").to_numpy(),
            "volume": pd.to_numeric(raw["v"], errors="coerce").to_numpy(),
        },
        index=pd.DatetimeIndex(ts, name="ts_event"),
    )
    return df


def convert(jsonl_path, out_path) -> dict:
    from fetch_databento import bars_to_loader_csv

    df = load_polygon_jsonl(jsonl_path)
    return bars_to_loader_csv(df, out_path)


def pull(jsonl_path) -> None:
    cmd = ["python3", str(POLY_CLI), "aggs", "--ticker", "SPY",
           "--from", PULL_FROM, "--to", PULL_TO,
           "--out", str(jsonl_path)]
    print("Pulling:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Polygon SPY 1m JSONL to the project CSV")
    parser.add_argument("--pull", action="store_true",
                        help="fetch fresh JSONL through the connector "
                             "CLI first (self-paced, several minutes)")
    parser.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)

    if args.pull:
        pull(Path(args.jsonl))
    stats = convert(Path(args.jsonl), Path(args.out))
    print(f"Wrote {stats['bars']:,} bars to {stats['path']}")
    print(f"  ET range: {stats['first']} -> {stats['last']}")
    print(f"  file size: {stats['bytes']:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
