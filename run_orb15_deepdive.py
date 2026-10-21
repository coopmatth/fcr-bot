#!/usr/bin/env python3
"""ORB-15 deep dive on the Polygon SPY tape (paper only).

Matt's redirect (2026-10-21): drop the pair portfolio; focus entirely
on ORB-15 SPY — the confirmed lead strategy (Polygon 24mo: 498
trades, +$6,803.40 after friction, PASS on all frozen criteria;
Databento agreed at +$7,157.36). No new API calls: everything here
works off data/spy_1m_polygon.csv and the run's trade log.

PART 1 — diagnostics on the existing 498-trade log
(results/trades_orb15_spy_poly.csv), all on an after-friction basis
(per-trade AF = pnl_usd - friction_usd): monthly + quarterly P&L,
long/short split, exit-reason mix, entry-time buckets, distribution
stats (avg win/loss, extremes, best/worst days, streaks, best-10-days
profit concentration), and the drawdown profile of the realized
after-friction equity curve (max DD with peak/trough dates, longest
underwater stretch in trading days, recovery).

PART 2 — pre-declared robustness (NO parameter tuning; the orb15
spec is untouched):
  (a) friction stress — re-runs at 2x ($0.08/share) and 3x
      ($0.12/share) round-trip friction via a SymbolMeta override
      (friction only scales the per-trade deduction in this engine;
      signals and sizes are unaffected). Frozen criteria at each.
  (b) execution-lag stress — entry_delay_bars=1: signals fill at
      the NEXT bar's open with signal-time stop/target levels
      (fcr.strategies_intraday stress harness). Frozen criteria.
  (c) sub-period stability — AF $ in five calendar buckets.
Every stress re-run first reproduces the baseline (0.04, delay 0)
and asserts it matches results/polygon_verdict.json exactly; a
mismatch aborts the run — no stress number is trusted from an
unverified route.

Output: results/orb15_deepdive.json (+ stress trade logs).

Run:  .venv/bin/python run_orb15_deepdive.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from collections import OrderedDict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from fcr.data import load_csv
from fcr.strategies_intraday import SymbolMeta, run_intraday_backtest
from run_polygon_spy import RUN_END
from run_strategies_intraday import EXTENDED_COLUMNS, SYMBOL_META
from run_validation24 import FULL_START, RESULTS, ROOT, enrich_from_csv, pass_test

ET = ZoneInfo("America/New_York")
STARTING_BALANCE = 500.0
BASE_FRICTION = 0.04
TRADE_LOG = RESULTS / "trades_orb15_spy_poly.csv"
CELL_JSON = RESULTS / "orb15_spy_poly.json"
DATA_CSV = ROOT / "data" / "spy_1m_polygon.csv"


def money(x: float) -> float:
    return round(float(x), 2)


def make_meta(friction: float) -> SymbolMeta:
    traded, _, _, pv, tick, cap, _f, itype = SYMBOL_META["spy"]
    return SymbolMeta(
        symbol=traded, point_value_usd=pv, tick_size=tick,
        max_units=cap, friction_per_unit_usd=friction,
        risk_budget_usd=150.0, instrument_type=itype,
    )


def settle(rows: list[dict], friction: float, tag: str) -> dict:
    """Halves + trough + frozen pass test for one stress run.

    Writes the rows to a scratch CSV and reuses validation24's
    enrich_from_csv so the arithmetic is byte-identical to the
    verified cells (rows already carry their own friction_usd).
    """
    scratch = Path(tempfile.mkdtemp(prefix="orb15dd_"))
    log = scratch / f"trades_{tag}.csv"
    pd.DataFrame(rows, columns=EXTENDED_COLUMNS).to_csv(log, index=False)
    enriched = enrich_from_csv(log, friction)
    net_af = money(sum(r["pnl_usd"] - r["friction_usd"] for r in rows))
    halves = enriched["halves"]
    trough = enriched["trough_after_friction_usd"]
    return {
        "trades": len(rows),
        "wins_gross": sum(1 for r in rows if r["pnl_usd"] > 0),
        "net_usd_after_friction": net_af,
        "halves": halves,
        "trough_after_friction_usd": trough,
        "trough_after_friction_at": enriched["trough_after_friction_at"],
        "pass": pass_test(net_af, halves, trough),
    }


def rerun(df: pd.DataFrame, friction: float, delay: int, tag: str,
          save_log: bool = False) -> dict:
    rows, days, stats = run_intraday_backtest(
        df, "orb15", make_meta(friction), start=FULL_START, end=RUN_END,
        starting_balance=STARTING_BALANCE, entry_delay_bars=delay)
    out = settle(rows, friction, tag)
    out.update({
        "trading_days": days,
        "net_usd_gross": stats["net_usd"],
        "net_r_gross": stats["net_r"],
        "max_drawdown_usd_gross": stats["max_drawdown_usd"],
        "skipped_for_size": stats["skipped_for_size"],
        "lag_voided": stats["lag_voided"],
        "total_friction_usd": stats["total_friction_usd"],
    })
    if save_log:
        suffix = tag.replace("orb15dd_", "")
        name = f"trades_orb15_spy_poly_{suffix}.csv"
        pd.DataFrame(rows, columns=EXTENDED_COLUMNS).to_csv(
            RESULTS / name, index=False)
        out["trade_log"] = f"results/{name}"
    return out


# ---------------------------------------------------------------------------
# PART 1 — diagnostics on the existing trade log
# ---------------------------------------------------------------------------
def load_af_trades() -> pd.DataFrame:
    t = pd.read_csv(TRADE_LOG)
    t["af_pnl_usd"] = (t["pnl_usd"] - t["friction_usd"]).round(2)
    t["win"] = t["af_pnl_usd"] > 0
    t = t.sort_values(["date", "exit_time", "entry_time"]).reset_index(drop=True)
    return t


def group_stats(t: pd.DataFrame) -> dict:
    n = len(t)
    return {
        "trades": int(n),
        "wins": int(t["win"].sum()) if n else 0,
        "win_rate": round(float(t["win"].mean()), 4) if n else None,
        "af_usd": money(t["af_pnl_usd"].sum()) if n else 0.0,
    }


def diagnostics(t: pd.DataFrame) -> dict:
    # monthly (every calendar month in the window, zero-trade months
    # shown explicitly) + quarterly rollup
    months = pd.period_range("2024-10", "2026-10", freq="M")
    t["_month"] = t["date"].str.slice(0, 7)
    monthly = []
    for m in months:
        monthly.append({"month": str(m), **group_stats(t[t["_month"] == str(m)])})
    t["_quarter"] = pd.PeriodIndex(
        pd.to_datetime(t["date"]).dt.to_period("Q").astype(str), freq="Q")
    quarters = sorted(t["_quarter"].unique())
    quarterly = [{"quarter": str(q), **group_stats(t[t["_quarter"] == q])}
                 for q in quarters]

    long_short = {
        side: group_stats(t[t["direction"] == side])
        for side in ("long", "short")
    }

    exit_reasons = {}
    for reason, sub in t.groupby("exit_reason"):
        exit_reasons[reason] = group_stats(sub)

    def bucket(hhmm: str) -> str:
        hh, mm = map(int, hhmm.split(":"))
        start_min = (mm // 30) * 30
        return f"{hh:02d}:{start_min:02d}"

    t["_bucket"] = t["entry_time"].map(bucket)
    entry_buckets = [
        {"bucket": b, **group_stats(t[t["_bucket"] == b])}
        for b in sorted(t["_bucket"].unique())
    ]

    wins_t, losses_t = t[t["win"]], t[~t["win"]]
    largest_win = wins_t.loc[wins_t["af_pnl_usd"].idxmax()] if len(wins_t) else None
    largest_loss = losses_t.loc[losses_t["af_pnl_usd"].idxmin()] if len(losses_t) else None

    daily = t.groupby("date")["af_pnl_usd"].sum()
    daily_counts = t.groupby("date").size()
    best_days = [{"date": d, "trades": int(daily_counts[d]), "af_usd": money(v)}
                 for d, v in daily.sort_values(ascending=False).head(5).items()]
    worst_days = [{"date": d, "trades": int(daily_counts[d]), "af_usd": money(v)}
                  for d, v in daily.sort_values().head(5).items()]
    top10 = daily.sort_values(ascending=False).head(10)
    total_net = money(t["af_pnl_usd"].sum())
    gross_profit = money(t.loc[t["af_pnl_usd"] > 0, "af_pnl_usd"].sum())

    # streaks in trade sequence (one trade/day, so also day streaks)
    best_run = {"win": 0, "loss": 0}
    run = {"win": 0, "loss": 0}
    prev = None
    for w in t["win"]:
        kind = "win" if w else "loss"
        run[kind] = run[kind] + 1 if prev == kind else 1
        best_run[kind] = max(best_run[kind], run[kind])
        prev = kind

    distribution = {
        "avg_win_usd": money(wins_t["af_pnl_usd"].mean()) if len(wins_t) else None,
        "avg_loss_usd": money(losses_t["af_pnl_usd"].mean()) if len(losses_t) else None,
        "largest_win": ({"date": largest_win["date"],
                         "af_usd": money(largest_win["af_pnl_usd"])}
                        if largest_win is not None else None),
        "largest_loss": ({"date": largest_loss["date"],
                          "af_usd": money(largest_loss["af_pnl_usd"])}
                         if largest_loss is not None else None),
        "best_days": best_days,
        "worst_days": worst_days,
        "longest_win_streak": best_run["win"],
        "longest_loss_streak": best_run["loss"],
        "best_10_days": {
            "af_usd": money(top10.sum()),
            "share_of_total_net_pct":
                round(100.0 * float(top10.sum()) / total_net, 1)
                if total_net > 0 else None,
            "share_of_gross_profit_pct":
                round(100.0 * float(top10.sum()) / gross_profit, 1)
                if gross_profit > 0 else None,
        },
    }

    # drawdown profile on the daily realized AF equity curve
    cell = json.loads(CELL_JSON.read_text())
    trading_days = [p["date"] for p in cell["equity_curve"]
                    if p["date"] != "start"]
    daily_map = {d: float(v) for d, v in daily.items()}
    equity = STARTING_BALANCE
    peak, peak_date = STARTING_BALANCE, "start"
    max_dd = 0.0
    dd_peak_date = dd_trough_date = "start"
    dd_peak_value = STARTING_BALANCE
    eq_by_day: list[tuple[str, float]] = []
    episodes: list[dict] = []
    ep: dict | None = None  # current underwater episode
    for d in trading_days:
        equity = money(equity + daily_map.get(d, 0.0))
        eq_by_day.append((d, equity))
        if equity >= peak:
            if ep is not None:
                ep["recovered"] = True
                ep["recovery_date"] = d
                episodes.append(ep)
                ep = None
            peak, peak_date = equity, d
        else:
            if ep is None:
                ep = {"trading_days": 0, "peak_date": peak_date,
                      "peak_equity_usd": money(peak),
                      "first_underwater_date": d,
                      "recovery_date": None, "recovered": False}
            ep["trading_days"] += 1
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
                dd_peak_value, dd_peak_date = peak, peak_date
                dd_trough_date = d
    if ep is not None:
        episodes.append(ep)
    longest_uw = (max(episodes, key=lambda e: e["trading_days"])
                  if episodes else
                  {"trading_days": 0, "peak_date": None,
                   "peak_equity_usd": None, "first_underwater_date": None,
                   "recovery_date": None, "recovered": True})
    # recovery of the max-DD episode specifically
    recovery_date = None
    days_peak_to_recovery = None
    if max_dd > 0:
        after = [d for d, eq in eq_by_day
                 if d > dd_trough_date and eq >= dd_peak_value]
        if after:
            recovery_date = after[0]
            span = [d for d, _ in eq_by_day
                    if dd_peak_date < d <= recovery_date]
            days_peak_to_recovery = len(span)
    drawdown = {
        "basis": "realized after-friction equity, close of each "
                 "trading day (project convention; no mark-to-market)",
        "max_drawdown_usd": money(max_dd),
        "peak_date": dd_peak_date,
        "peak_equity_usd": money(dd_peak_value),
        "trough_date": dd_trough_date,
        "trough_equity_usd": money(dd_peak_value - max_dd),
        "recovery_date": recovery_date,
        "trading_days_peak_to_recovery": days_peak_to_recovery,
        "longest_underwater": longest_uw,
        "af_trough_usd": money(min(eq for _, eq in eq_by_day)),
    }
    return {
        "monthly": monthly,
        "quarterly": quarterly,
        "long_short": long_short,
        "exit_reasons": exit_reasons,
        "entry_time_buckets": entry_buckets,
        "distribution": distribution,
        "drawdown": drawdown,
    }


# ---------------------------------------------------------------------------
# PART 2c — sub-period stability (baseline log)
# ---------------------------------------------------------------------------
SUBPERIODS = [
    ("2024H2_partial", date(2024, 10, 21), date(2024, 12, 31)),
    ("2025H1", date(2025, 1, 1), date(2025, 6, 30)),
    ("2025H2", date(2025, 7, 1), date(2025, 12, 31)),
    ("2026H1", date(2026, 1, 1), date(2026, 6, 30)),
    ("2026H2_partial", date(2026, 7, 1), date(2026, 10, 19)),
]


def subperiods(t: pd.DataFrame) -> dict:
    dates = pd.to_datetime(t["date"]).dt.date
    buckets = []
    for name, lo, hi in SUBPERIODS:
        sub = t[(dates >= lo) & (dates <= hi)]
        s = group_stats(sub)
        buckets.append({"bucket": name,
                        "window": f"{lo.isoformat()}..{hi.isoformat()}",
                        **s, "green": s["af_usd"] > 0})
    return {"buckets": buckets,
            "green_count": sum(1 for b in buckets if b["green"]),
            "bucket_count": len(buckets)}


# ---------------------------------------------------------------------------
def main() -> int:
    verdict = json.loads((RESULTS / "polygon_verdict.json").read_text())
    ref = next(c["polygon"] for c in verdict["cells"]
               if c["strategy"] == "orb15")
    df = load_csv(DATA_CSV)

    # Baseline reproduction FIRST — no stress number without it.
    base = rerun(df, BASE_FRICTION, 0, "orb15dd_base")
    checks = {
        "trades": base["trades"] == ref["trades"],
        "net_af": base["net_usd_after_friction"]
                  == ref["net_usd_after_friction"],
        "h1_af": base["halves"]["h1"]["net_after_friction_usd"]
                 == ref["halves"]["h1"]["net_after_friction_usd"],
        "h2_af": base["halves"]["h2"]["net_after_friction_usd"]
                 == ref["halves"]["h2"]["net_after_friction_usd"],
        "trough": base["trough_after_friction_usd"]
                  == ref["trough_after_friction_usd"],
    }
    if not all(checks.values()):
        print(f"BASELINE REPRODUCTION FAILED: {checks}", file=sys.stderr)
        print(f"  got {base['trades']}t AF "
              f"${base['net_usd_after_friction']:+.2f} trough "
              f"${base['trough_after_friction_usd']:.2f}; expected "
              f"{ref['trades']}t AF ${ref['net_usd_after_friction']:+.2f} "
              f"trough ${ref['trough_after_friction_usd']:.2f}",
              file=sys.stderr)
        return 1
    print(f"Baseline reproduced: {base['trades']} trades, AF "
          f"${base['net_usd_after_friction']:+.2f}, trough "
          f"${base['trough_after_friction_usd']:.2f} — matches "
          f"polygon_verdict.json.")

    t = load_af_trades()
    diag = diagnostics(t)

    fric_stress = [{"label": "1x (frozen)", "multiple": 1.0,
                    "friction_per_unit_usd": BASE_FRICTION, **base}]
    for mult, fric, tag in ((2.0, 0.08, "fric2x"), (3.0, 0.12, "fric3x")):
        r = rerun(df, fric, 0, f"orb15dd_{tag}", save_log=True)
        fric_stress.append({"label": f"{mult:.0f}x", "multiple": mult,
                            "friction_per_unit_usd": fric, **r})
        print(f"Friction {mult:.0f}x (${fric:.2f}/sh): {r['trades']}t "
              f"AF ${r['net_usd_after_friction']:+.2f} "
              f"H2 ${r['halves']['h2']['net_after_friction_usd']:+.2f} "
              f"trough ${r['trough_after_friction_usd']:.2f} -> "
              f"{'PASS' if r['pass']['passed'] else 'FAIL'}")

    lag = rerun(df, BASE_FRICTION, 1, "orb15dd_lag1", save_log=True)
    print(f"Lag 1 bar: {lag['trades']}t ({lag['lag_voided']} voided, "
          f"{lag['skipped_for_size']} skipped) AF "
          f"${lag['net_usd_after_friction']:+.2f} "
          f"H2 ${lag['halves']['h2']['net_after_friction_usd']:+.2f} "
          f"trough ${lag['trough_after_friction_usd']:.2f} -> "
          f"{'PASS' if lag['pass']['passed'] else 'FAIL'}")

    subs = subperiods(t)
    green = ", ".join(
        f"{b['bucket']} ${b['af_usd']:+.2f}" for b in subs["buckets"])
    print(f"Sub-periods: {green} ({subs['green_count']}/"
          f"{subs['bucket_count']} green)")

    doc = OrderedDict([
        ("generated_at",
         datetime.now(ET).isoformat(timespec="seconds")),
        ("strategy", "orb15"),
        ("strategy_label",
         "ORB 15-min opening range breakout (2R target)"),
        ("symbol", "SPY"),
        ("data_file", "data/spy_1m_polygon.csv"),
        ("data_note", "Polygon.io consolidated (SIP) 1-minute "
                      "aggregates, adjusted=false (raw prices)"),
        ("window", {"start": FULL_START.isoformat(),
                    "end": RUN_END.isoformat()}),
        ("basis", "All diagnostics on an after-friction basis: "
                  "per-trade AF = pnl_usd - friction_usd at the "
                  "frozen $0.04/share round trip. A trade counts as "
                  "a win when its AF pnl > 0 (the headline cell "
                  "win rate, 43.0%, is on gross pnl). Equity starts "
                  "at $500; one trade per day by construction."),
        ("baseline_check", {"matched_polygon_verdict": True,
                            "checks": checks}),
        ("baseline", {"trades": base["trades"],
                      "net_usd_after_friction":
                          base["net_usd_after_friction"],
                      "halves": base["halves"],
                      "trough_after_friction_usd":
                          base["trough_after_friction_usd"],
                      "pass": base["pass"]}),
        ("diagnostics", diag),
        ("robustness", {
            "friction_stress": fric_stress,
            "execution_lag": {
                "delay_bars": 1,
                "fill": "next bar's open; signal-time stop/target "
                        "levels; units re-sized at the fill price; "
                        "an open already past the stop or target "
                        "voids the trade",
                **lag},
            "subperiods": subs,
        }),
    ])
    out = RESULTS / "orb15_deepdive.json"
    out.write_text(json.dumps(doc, indent=2))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
