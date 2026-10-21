#!/usr/bin/env python3
"""REAL-QUOTES rerun of the ORB-15 options variant (paper only).

Replaces the Black-Scholes model of run_orb15_options_modeled.py
with REAL historical NBBO quotes from Databento's OPRA.PILLAR
dataset (schema cbbo-1m: consolidated best bid/offer sampled at
1-minute intervals, available from April 2013).

Method (frozen for this run):
  * Trade list: the IDENTICAL 498 frozen ORB-15 trades of the
    Polygon verdict (results/trades_orb15_spy_poly.csv — the same
    file the modeled run asserted row-identical to a regeneration).
  * Contract (ORB15_OPTIONS_SPEC.md): expiry = the trade date
    (0DTE); strike = nearest $1 strike to the entry price;
    long -> CALL, short -> PUT; exactly 1 contract. OCC raw symbol
    (fcr/options_real.occ_raw_symbol).
  * Quotes: cbbo-1m rows for exactly those contracts, each on its
    own trading day. Matching rules live in fcr/options_real.py:
    entry = ask at the entry minute (nearest row within +/-2 min,
    flagged); REAL premium gate: ask > $2.50/share -> day SKIPPED;
    no usable ask -> UNTRADEABLE (counted separately). Exit = bid
    at the exit minute (same +/-2 rule), else last bid before
    16:00 (flagged), else 0.00 worthless (flagged).
    Convention: the cbbo-1m row stamped with an ET wall-clock
    minute is matched to the trade minute of the same stamp.
  * P&L/trade = (exit bid - entry ask) x 100. $500 overlay,
    sequential, per window (full 24mo; last 120 trading days),
    exactly like the modeled run.
  * If a requested contract returns ZERO rows (strike not listed
    that day), the nearest LISTED strike is found via the
    definition schema for that date and pulled instead; every
    substitution is logged and counted.

SPEND GUARDRAIL: the metadata cost of the whole request is
quoted BEFORE any download (metadata calls are free). If the
total quoted cost exceeds $25, NOTHING is downloaded and the
script exits after printing the quote.

The Databento API key is read ONLY from the DATABENTO_API_KEY
environment variable. It is never printed, logged, or written
to any file. Error messages are scrubbed of it defensively.

Run:
  DATABENTO_API_KEY=... .venv/bin/python run_orb15_options_real.py --quote-only
  DATABENTO_API_KEY=... .venv/bin/python run_orb15_options_real.py
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
DATA = ROOT / "data"
FROZEN_TRADES = RESULTS / "trades_orb15_spy_poly.csv"
MODELED = RESULTS / "orb15_options_modeled.json"
OUT = RESULTS / "orb15_options_real.json"
DBN_PATH = DATA / "opra_spy_orb15_cbbo1m.dbn.zst"
QUOTES_CSV = DATA / "opra_spy_orb15_cbbo1m.csv"
TAPE = DATA / "spy_1m_polygon.csv"

DATASET = "OPRA.PILLAR"
SCHEMA_PREFERENCE = ["cbbo-1m", "cmbp-1", "tcbbo"]
SPEND_CAP_USD = 25.0
STARTING_BALANCE = 500.0
RUN_END = date(2026, 10, 19)
ET = "America/New_York"


def money(x: float) -> float:
    return round(x + 0.0, 2)


# ---------------------------------------------------------------------------
# Frozen trade list -> contract manifest
# ---------------------------------------------------------------------------
def build_manifest() -> list[dict]:
    from fcr.options_real import occ_raw_symbol
    with FROZEN_TRADES.open() as fh:
        trades = list(csv.DictReader(fh))
    assert len(trades) == 498, len(trades)
    manifest = []
    for t in trades:
        d = date.fromisoformat(t["date"])
        kind = "call" if t["direction"] == "long" else "put"
        strike = float(round(float(t["entry_price"])))
        eh, em = map(int, t["entry_time"].split(":"))
        xh, xm = map(int, t["exit_time"].split(":"))
        manifest.append({
            "date": t["date"], "trade_date": d,
            "direction": t["direction"], "kind": kind,
            "strike": strike,
            "symbol": occ_raw_symbol("SPY", d, kind, strike),
            "entry_minute": eh * 60 + em, "exit_minute": xh * 60 + xm,
            "entry_time": t["entry_time"], "exit_time": t["exit_time"],
            "exit_reason": t["exit_reason"],
            "shares_pnl_after_friction":
                money(float(t["pnl_usd"]) - float(t["friction_usd"])),
        })
    return manifest


# ---------------------------------------------------------------------------
# Databento plumbing (key from env only; errors scrubbed)
# ---------------------------------------------------------------------------
def make_client():
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        print("FATAL: DATABENTO_API_KEY not set in environment")
        sys.exit(2)
    import databento as db
    return db.Historical(key)


def scrub(text: str) -> str:
    key = os.environ.get("DATABENTO_API_KEY", "")
    return text.replace(key, "***") if key else text


def api_call(fn, *args, **kwargs):
    """Call a Databento API function; on ANY auth-class rejection
    stop the whole job immediately (parent instruction: after the
    FIRST auth rejection of any kind, make no further API calls).
    Databento returns auth_invalid_username_in_basic_auth as
    HTTP 400, not 401 — keying on 401 alone caused a second
    refused call in the 2026-10-21 second attempt."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        status = getattr(exc, "status", None) or \
            getattr(exc, "http_status", None)
        msg = scrub(str(exc))
        if status == 401 or "401" in msg or "auth_invalid" in msg \
                or "invalid username" in msg.lower():
            print(f"AUTH_FAILED: Databento rejected the API key "
                  f"(HTTP {status}). Stopping immediately; no "
                  f"further API calls, no download attempted.")
            sys.exit(3)
        print(f"API_ERROR: {type(exc).__name__}: {msg[:400]}")
        raise


def pick_schema(client) -> str:
    try:
        available = api_call(client.metadata.list_schemas, DATASET)
    except SystemExit:
        raise
    except Exception:
        available = None
    if available:
        for s in SCHEMA_PREFERENCE:
            if s in available:
                return s
        print(f"none of {SCHEMA_PREFERENCE} in OPRA.PILLAR schemas: "
              f"{available}")
        sys.exit(4)
    return SCHEMA_PREFERENCE[0]  # get_cost will validate


def quote(client, schema, symbols, start, end, stype_in="raw_symbol"):
    cost = api_call(client.metadata.get_cost, dataset=DATASET,
                    start=start, end=end, symbols=symbols,
                    schema=schema, stype_in=stype_in)
    count = api_call(client.metadata.get_record_count, dataset=DATASET,
                     start=start, end=end, symbols=symbols,
                     schema=schema, stype_in=stype_in)
    return float(cost), int(count)


def parse_dbn(path: Path, csv_mode: str | None = "write"):
    """Parse a CBBO DBN file into {(symbol, et_date): [QuoteRow]}.
    Optionally writes/appends the parsed frame to QUOTES_CSV."""
    import databento as db
    import pandas as pd
    from fcr.options_real import QuoteRow, clean_price
    store = db.read_dbn(path)
    df = store.to_df()
    rows: dict[tuple[str, str], list[QuoteRow]] = {}
    if len(df) == 0:
        return rows
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    et = idx.tz_convert(ET)
    minutes = et.hour * 60 + et.minute
    dates = et.strftime("%Y-%m-%d")
    frame = pd.DataFrame({
        "symbol": df["symbol"].astype(str).values,
        "et_date": dates, "minute": minutes,
        "bid": df["bid_px_00"].values, "ask": df["ask_px_00"].values,
        "bid_sz": df["bid_sz_00"].values, "ask_sz": df["ask_sz_00"].values,
        "_ts": idx,
    })
    frame = frame[(frame["minute"] >= 570) & (frame["minute"] <= 960)]
    if csv_mode == "write":
        frame.to_csv(QUOTES_CSV, index=False)
    elif csv_mode == "append":
        frame.to_csv(QUOTES_CSV, mode="a", header=False, index=False)
    for rec in frame.itertuples(index=False):
        key = (rec.symbol, rec.et_date)
        rows.setdefault(key, []).append(QuoteRow(
            minute=int(rec.minute), bid=clean_price(rec.bid),
            ask=clean_price(rec.ask)))
    for v in rows.values():
        v.sort(key=lambda r: r.minute)
    return rows


def pull_quotes(client, schema, symbols, start, end, path: Path,
                csv_mode: str | None = "write"):
    """Download to DBN at `path`, return parsed rows."""
    api_call(client.timeseries.get_range, dataset=DATASET,
             start=start, end=end, symbols=symbols, schema=schema,
             stype_in="raw_symbol", path=path)
    return parse_dbn(path, csv_mode=csv_mode)


def listed_strikes(client, day: date, kind: str) -> tuple[float, int, list]:
    """Definition-schema lookup: (cost, record_count, sorted listed
    strikes) for SPY options expiring on `day` of the given kind."""
    import pandas as pd
    start = pd.Timestamp(day.isoformat(), tz=ET)
    end = start + timedelta(days=1)
    cost, count = quote(client, "definition", ["SPY.OPT"], start, end,
                        stype_in="parent")
    tmp = DATA / f"_defs_{day.isoformat()}.dbn.zst"
    api_call(client.timeseries.get_range, dataset=DATASET,
             start=start, end=end, symbols=["SPY.OPT"],
             schema="definition", stype_in="parent", path=tmp)
    import databento as db
    df = db.read_dbn(tmp).to_df()
    tmp.unlink(missing_ok=True)
    exp = pd.to_datetime(df["expiration"], utc=True)
    exp_dates = exp.dt.tz_convert(ET).dt.date
    cls = "C" if kind == "call" else "P"
    strikes = sorted({float(s) for s, e, c in
                      zip(df["strike_price"], exp_dates,
                          df["instrument_class"])
                      if e == day and str(c).upper().startswith(cls)})
    return cost, count, strikes


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def window_stats(records: list[dict]) -> dict:
    equity = peak = trough = STARTING_BALANCE
    max_dd = 0.0
    taken = [r for r in records if r["entry_status"] == "ok"]
    for r in records:
        if r["entry_status"] != "ok":
            continue
        equity += r["options_pnl"]
        peak = max(peak, equity)
        trough = min(trough, equity)
        max_dd = max(max_dd, peak - equity)
    wins = [r["options_pnl"] for r in taken if r["options_pnl"] > 0]
    losses = [r["options_pnl"] for r in taken if r["options_pnl"] <= 0]
    return {
        "trades_total": len(records),
        "trades_taken": len(taken),
        "gate_skipped": sum(1 for r in records
                            if r["entry_status"] == "gate"),
        "untradeable": sum(1 for r in records
                           if r["entry_status"] == "untradeable"),
        "win_rate": round(len(wins) / len(taken), 4) if taken else None,
        "net_pnl_usd": money(sum(r["options_pnl"] for r in taken)),
        "ending_overlay_usd": money(equity),
        "trough_usd": money(trough),
        "max_drawdown_usd": money(max_dd),
        "avg_win_usd": money(statistics.mean(wins)) if wins else None,
        "avg_loss_usd": money(statistics.mean(losses)) if losses else None,
        "best_trade": max(({"date": r["date"],
                             "options_pnl": r["options_pnl"]}
                            for r in taken),
                          key=lambda x: x["options_pnl"], default=None),
        "worst_trade": min(({"date": r["date"],
                             "options_pnl": r["options_pnl"]}
                            for r in taken),
                           key=lambda x: x["options_pnl"], default=None),
    }


def decay_diagnostic(records: list[dict]) -> dict:
    taken = [r for r in records if r["entry_status"] == "ok"]
    sw = [r for r in taken if r["shares_pnl_after_friction"] > 0]
    sl = [r for r in taken if r["shares_pnl_after_friction"] <= 0]
    return {
        "basis": "trades TAKEN under real quotes",
        "shares_winners": len(sw),
        "shares_winners_that_lost_in_options":
            sum(1 for r in sw if r["options_pnl"] < 0),
        "shares_losers": len(sl),
        "shares_losers_that_won_in_options":
            sum(1 for r in sl if r["options_pnl"] > 0),
    }


def last120_start() -> date:
    """Replicates the modeled run's window boundary: the 120th-from-
    last RTH trading day of the Polygon tape within the window."""
    from fcr.data import load_csv
    from run_validation24 import FULL_START
    df = load_csv(TAPE)
    ts = df["timestamp"]
    from datetime import time as dtime
    rth = df[(ts.dt.time >= dtime(9, 30)) & (ts.dt.time < dtime(16, 0))]
    days = sorted({t.date() for t in rth["timestamp"]
                   if FULL_START <= t.date() <= RUN_END})
    return days[-120]


def main() -> int:
    import pandas as pd
    quote_only = "--quote-only" in sys.argv
    manifest = build_manifest()
    symbols = sorted({m["symbol"] for m in manifest})
    start = pd.Timestamp(manifest[0]["date"], tz=ET)
    end = pd.Timestamp(manifest[-1]["date"], tz=ET) + timedelta(days=1)
    print(f"manifest: {len(manifest)} trades, {len(symbols)} unique "
          f"contracts, {manifest[0]['date']} .. {manifest[-1]['date']}")

    if "--from-dbn" in sys.argv:
        # Recovery mode: the paid download already sits at DBN_PATH.
        # Parse it with NO further API calls (cost/count passed in
        # from the original quote via --cost=/--count=). No client,
        # so no strike substitutions are attempted in this mode.
        client = None
        schema = "cbbo-1m"
        cost = float(next((a.split("=", 1)[1] for a in sys.argv
                           if a.startswith("--cost=")), 0.0))
        count = int(next((a.split("=", 1)[1] for a in sys.argv
                          if a.startswith("--count=")), 0))
        rows = parse_dbn(DBN_PATH)
        print(f"from-dbn mode: parsed "
              f"{sum(len(v) for v in rows.values())} rows from "
              f"{DBN_PATH.name}; no API calls made")
    else:
        client = make_client()
        schema = pick_schema(client)
        print(f"schema: {schema} (dataset {DATASET})")
        cost, count = quote(client, schema, symbols, start, end)
        print(f"COST QUOTE: ${cost:.4f} for {count:,} records "
              f"({len(symbols)} contracts x full session cbbo-1m)")
        if quote_only:
            print(json.dumps({"schema": schema,
                              "cost_usd": round(cost, 4),
                              "record_count": count,
                              "contracts": len(symbols)}))
            return 0
        if cost > SPEND_CAP_USD:
            print(f"SPEND GUARDRAIL: quoted ${cost:.2f} > "
                  f"${SPEND_CAP_USD} cap — downloading NOTHING.")
            return 5
        if schema != "cbbo-1m":
            print("NOTE: cbbo-1m unavailable; fell back per brief, "
                  "and the fallback quote is within the cap.")
        rows = pull_quotes(client, schema, symbols, start, end,
                           DBN_PATH)
        total_rows = sum(len(v) for v in rows.values())
        print(f"downloaded: {total_rows:,} session rows across "
              f"{len(rows)} contract-days")

    # ---- zero-row contracts: nearest-listed-strike substitution ----
    substitutions = []
    extra_cost = 0.0
    missing = [m for m in manifest
               if (m["symbol"], m["date"]) not in rows]
    if client is None and missing:
        for m in missing:
            substitutions.append({
                "date": m["date"], "symbol": m["symbol"],
                "strike": m["strike"], "substituted_symbol": None,
                "reason": "no rows in downloaded DBN; from-dbn mode "
                          "makes no API calls for substitution"})
        missing = []
    for m in missing:
        dcost, _dc, strikes = listed_strikes(client, m["trade_date"],
                                             m["kind"])
        extra_cost += dcost
        if not strikes:
            substitutions.append({**{k: m[k] for k in
                                     ("date", "symbol", "strike")},
                                  "substituted_symbol": None,
                                  "reason": "no listed strikes in "
                                            "definitions"})
            continue
        near = min(strikes, key=lambda s: (abs(s - m["strike"]), s))
        from fcr.options_real import occ_raw_symbol
        sub_symbol = occ_raw_symbol("SPY", m["trade_date"], m["kind"],
                                    near)
        day_start = pd.Timestamp(m["date"], tz=ET)
        ccost, _cc = quote(client, schema, [sub_symbol], day_start,
                           day_start + timedelta(days=1))
        extra_cost += ccost
        if cost + extra_cost > SPEND_CAP_USD:
            substitutions.append({**{k: m[k] for k in
                                     ("date", "symbol", "strike")},
                                  "substituted_symbol": None,
                                  "reason": "substitution would exceed "
                                            "spend cap"})
            continue
        sub_path = DATA / f"_sub_{m['date']}.dbn.zst"
        sub_rows = pull_quotes(client, schema, [sub_symbol], day_start,
                               day_start + timedelta(days=1), sub_path,
                               csv_mode="append")
        sub_path.unlink(missing_ok=True)
        got = sub_rows.get((sub_symbol, m["date"]), [])
        if got:
            rows[(sub_symbol, m["date"])] = got
        substitutions.append({
            "date": m["date"], "symbol": m["symbol"],
            "strike": m["strike"], "substituted_symbol": sub_symbol,
            "substituted_strike": near, "rows": len(got)})
        m["used_symbol"] = sub_symbol
        m["used_strike"] = near
    if missing:
        print(f"zero-row contracts: {len(missing)}; substitutions: "
              f"{substitutions}")

    # ---- per-trade matching ----------------------------------------
    from fcr.options_real import entry_quote, exit_quote
    modeled = json.loads(MODELED.read_text())
    modeled_a = {}
    for w in modeled["windows"].values():
        for t in w["anchors"]["realized"]["trades"]:
            modeled_a[t["date"]] = t["entry_ask"]
    modeled_gate_dates = set(
        modeled["windows"]["full_24mo"]["anchors"]["realized"]
        ["skipped_dates"])

    records = []
    flag_counts: dict[str, int] = {}
    for m in manifest:
        symbol = m.get("used_symbol", m["symbol"])
        day_rows = rows.get((symbol, m["date"]), [])
        ent = entry_quote(day_rows, m["entry_minute"])
        rec = {
            "date": m["date"], "direction": m["direction"],
            "kind": m["kind"], "occ_symbol": symbol,
            "strike": m.get("used_strike", m["strike"]),
            "strike_substituted": "used_symbol" in m,
            "entry_time": m["entry_time"], "exit_time": m["exit_time"],
            "exit_reason": m["exit_reason"],
            "shares_pnl_after_friction": m["shares_pnl_after_friction"],
            "entry_status": ent["status"], "entry_ask": ent["ask"],
            "entry_minute_used": ent["minute"],
            "entry_delta_min": ent["delta"],
            "modeled_a_entry_ask": modeled_a.get(m["date"]),
            "flags": list(ent["flags"]),
        }
        if ent["status"] == "ok":
            ex = exit_quote(day_rows, m["exit_minute"], m["entry_minute"])
            rec["exit_bid"] = ex["bid"]
            rec["exit_minute_used"] = ex["minute"]
            rec["exit_delta_min"] = ex["delta"]
            rec["flags"] += ex["flags"]
            rec["options_pnl"] = money((ex["bid"] - ent["ask"]) * 100.0)
        else:
            rec["exit_bid"] = None
            rec["exit_minute_used"] = None
            rec["exit_delta_min"] = None
            rec["options_pnl"] = None
        for f in rec["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1
        records.append(rec)

    l120 = last120_start()
    windows = {
        "full_24mo": records,
        "last_120d": [r for r in records
                      if date.fromisoformat(r["date"]) >= l120],
    }
    doc = {
        "generated_for": "Matt's follow-up 2026-10-21: rerun the "
                         "ORB-15 options variant on REAL historical "
                         "quotes (Databento OPRA) instead of the "
                         "Black-Scholes model.",
        "honesty_banner": "Premiums here are REAL NBBO quotes "
                          "(OPRA via Databento), not model output. "
                          "Quotes are 1-minute sampled consolidated "
                          "BBO (cbbo-1m); the row stamped with an ET "
                          "wall-clock minute is matched to the "
                          "trade minute of the same stamp, with a "
                          "+/-2 minute flagged fallback. A sampled "
                          "quote is not a guaranteed fill: real fills "
                          "can be worse. The Alpaca options paper "
                          "forward test remains the arbiter.",
        "dataset": DATASET, "schema": schema,
        "cost": {"main_quote_usd": round(cost, 4),
                 "main_record_count": count,
                 "definitions_and_substitutions_quote_usd":
                     round(extra_cost, 4),
                 "total_quoted_usd": round(cost + extra_cost, 4),
                 "spend_cap_usd": SPEND_CAP_USD},
        "request_shape": {
            "symbols": "the specific 0DTE contract the frozen spec "
                       "selects for each of the 498 trades (nearest "
                       "$1 strike to entry, call if long else put)",
            "per_contract_range": "its trading day 09:30-16:00 ET",
            "raw_files_local_only": [DBN_PATH.name, QUOTES_CSV.name]},
        "strike_substitutions": substitutions,
        "flag_counts": flag_counts,
        "windows": {},
        "trades": records,
    }
    for wname, wrecs in windows.items():
        ratios = [r["entry_ask"] / r["modeled_a_entry_ask"] for r in wrecs
                  if r["entry_ask"] and r["modeled_a_entry_ask"]]
        ratios.sort()
        gate_both = sum(1 for r in wrecs if r["entry_status"] == "gate"
                        and r["date"] in modeled_gate_dates)
        gate_real_only = sum(1 for r in wrecs
                             if r["entry_status"] == "gate"
                             and r["date"] not in modeled_gate_dates)
        w = {"real_quotes": window_stats(wrecs),
             "decay_diagnostic_real": decay_diagnostic(wrecs),
             "real_vs_modeled_a_entry_ask": {
                 "basis": "trades with both a real entry ask (taken "
                          "or gate-skipped) and a modeled (a) ask",
                 "n": len(ratios),
                 "median_ratio": round(ratios[len(ratios) // 2], 4)
                 if ratios else None,
                 "p25_ratio": round(ratios[len(ratios) // 4], 4)
                 if ratios else None,
                 "p75_ratio": round(ratios[3 * len(ratios) // 4], 4)
                 if ratios else None,
                 "mean_ratio": round(statistics.mean(ratios), 4)
                 if ratios else None,
                 "gate_overlap": {"both_gated": gate_both,
                                  "real_only_gated": gate_real_only}}}
        doc["windows"][wname] = w

    OUT.write_text(json.dumps(doc, indent=1))
    print(f"\nwrote {OUT}")
    for wname, w in doc["windows"].items():
        s = w["real_quotes"]
        dd = w["decay_diagnostic_real"]
        print(f"===== {wname} ===== taken {s['trades_taken']} "
              f"gate {s['gate_skipped']} untradeable {s['untradeable']} "
              f"WR {s['win_rate']} net ${s['net_pnl_usd']:+,.2f} end "
              f"${s['ending_overlay_usd']:,.2f} trough "
              f"${s['trough_usd']:,.2f} maxDD ${s['max_drawdown_usd']:,.2f} "
              f"avgW {s['avg_win_usd']} avgL {s['avg_loss_usd']}")
        print(f"  decay: {dd['shares_winners_that_lost_in_options']} of "
              f"{dd['shares_winners']} shares-winners lost in options; "
              f"{dd['shares_losers_that_won_in_options']} of "
              f"{dd['shares_losers']} shares-losers won in options")
        r = w["real_vs_modeled_a_entry_ask"]
        print(f"  real/modeled(a) ask ratio: n={r['n']} median "
              f"{r['median_ratio']} p25 {r['p25_ratio']} p75 "
              f"{r['p75_ratio']} mean {r['mean_ratio']}; gate overlap "
              f"{r['gate_overlap']}")
    print(f"flags: {flag_counts}")
    print(f"substitutions: {len(substitutions)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
