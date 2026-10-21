#!/usr/bin/env python3
"""ORB-15 on MES — 24-month futures validation (paper only).

Question (the gate for Matt's live-$500 decision): does the frozen
ORB-15 edge survive on the futures tape, where the 9:30 ET 'open' is
an overnight continuation rather than SPY's cash auction open? That
effect halved ib_break's per-trade edge on MES in the cross-check,
so a SPY-only confirmation is not enough to take the futures form
live.

DATA: data/es_1m_24mo.csv — ES.v.0 tape, Databento GLBX.MDP3,
verified deterministic (the lab reproduces to the cent on it).
Traded as MES: $5/pt, tick 0.25, friction $4.90/contract round trip.

WINDOW: 2024-10-21 -> 2026-10-19 — the same trading days as the
SPY Polygon (SIP) arbiter validation (results/orb15_spy_poly.json).

STRATEGY: orb15 exactly as frozen in ORB15_SPEC.md
(ORBStrategy(minutes=15): 09:30-09:45 ET range, first close beyond
before 12:00, stop = opposite edge, target 2R, 15:55 flatten).

SIZING — the futures convention from Matt's futures spec, used
across the lab and DIFFERENT from the SPY run's uncapped shares by
design: contracts = min(5, floor($150 / (stop_points x $5/pt))).
0 contracts (a stop wider than 30 ES points) = skipped-for-size:
the signal has fired, the day is consumed, no second attempt.

STEP 1  Slice verification: orb15/MES restricted to the lab window
        (2026-06-22 -> 2026-10-20) on the 24-month ES file must
        EXACTLY match results/strategy_lab.json (orb15_mes_120d:
        74 trades, +$571.25 gross, +$46.95 AF). Any mismatch aborts
        before a single 24-month number is produced.
STEP 2  Full 24-month run + frozen pass test, identical to SPY:
        (a) 24m net-after-friction > 0, (b) H2 (2025-10-21 -> end)
        net-after-friction > 0, (c) after-friction realized-equity
        trough > $0 (enrich_from_csv semantics, as applied to both
        the SPY Polygon verdict and the MES cross-check).
STEP 3  Decision diagnostics: sub-period decay (the five calendar
        buckets of run_orb15_deepdive.py), monthly green/red,
        long/short split, stop-distance -> contract-count shape,
        and the side-by-side per-trade edge vs SPY Polygon.

Outputs: results/orb15_mes_24mo.json, results/trades_orb15_mes_24mo.csv,
results/orb15_mes_validation.json.

Run:  .venv/bin/python run_orb15_mes_validation.py
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fcr.data import load_csv
from run_validation24 import (
    FULL_START,
    RESULTS,
    ROOT,
    SLICE_END,
    SLICE_START,
    enrich_from_csv,
    intraday_run,
    money,
    pass_test,
)

ET = ZoneInfo("America/New_York")
STARTING_BALANCE = 500.0
FULL_END = date(2026, 10, 19)  # match the SPY Polygon window exactly

ES_CSV, ES_NOTE = "es_1m_24mo.csv", "ES.v.0 tape (GLBX.MDP3)"
DEGRADED_DAYS = ["2025-09-17", "2025-09-24", "2025-11-28"]
SIZING_RULE = (
    "contracts = min(5, floor($150 / (stop_points x $5/pt))) — "
    "Matt's futures sizing convention from the lab; 0 contracts "
    "(stop wider than 30 ES points) = skipped-for-size, day "
    "consumed. Friction $4.90/contract round trip. This differs "
    "from the SPY run's uncapped shares = floor($150 / stop_$) "
    "by design.")
FRAMING_NOTE = (
    "MES and SPY are the same index, but ES futures trade nearly "
    "around the clock, so the 9:30 ET 'open' for MES is a "
    "continuation of overnight trading rather than SPY's auction "
    "open. A strategy keyed to the cash open can legitimately "
    "behave differently on the two tapes; this run is the gate "
    "for the live-$500 decision because MES is the only faithful "
    "executable form of ORB-15 at that account size.")

# The deep dive's five calendar buckets (run_orb15_deepdive.py).
SUBPERIODS = [
    ("2024H2_partial", date(2024, 10, 21), date(2024, 12, 31)),
    ("2025H1", date(2025, 1, 1), date(2025, 6, 30)),
    ("2025H2", date(2025, 7, 1), date(2025, 12, 31)),
    ("2026H1", date(2026, 1, 1), date(2026, 6, 30)),
    ("2026H2_partial", date(2026, 7, 1), date(2026, 10, 19)),
]


def slice_check(df, lab_by_id, scratch: Path) -> dict:
    ref = lab_by_id["orb15_mes_120d"]
    rec = intraday_run("orb15", "mes", df, ES_CSV, ES_NOTE,
                       SLICE_START, SLICE_END,
                       "orb15mes_slice_check", scratch)
    got = {"trades": rec["trades"], "net_usd": rec["net_usd"],
           "net_after_friction": rec["net_usd_after_friction"]}
    expected = {"trades": ref["trades"], "net_usd": ref["net_usd"],
                "net_after_friction": ref["net_after_friction_usd"]}
    match = (got["trades"] == expected["trades"]
             and money(got["net_usd"]) == money(expected["net_usd"])
             and money(got["net_after_friction"])
             == money(expected["net_after_friction"]))
    print(f"SLICE orb15 mes: expected {expected['trades']}t "
          f"${expected['net_usd']:+.2f} / "
          f"${expected['net_after_friction']:+.2f} AF | got "
          f"{got['trades']}t ${got['net_usd']:+.2f} / "
          f"${got['net_after_friction']:+.2f} AF -> "
          f"{'MATCH' if match else 'MISMATCH'}")
    return {"cell": "orb15_mes", "lab_run_id": "orb15_mes_120d",
            "window": {"start": SLICE_START.isoformat(),
                       "end": SLICE_END.isoformat()},
            "expected": expected, "got": got, "match": match}


def analyze_trades(csv_path: Path, friction_per_unit: float) -> dict:
    """Decay / shape diagnostics from the written trade CSV.

    AF pnl per trade = pnl_usd - friction_usd (the runner writes
    friction_usd = contracts x friction_per_unit). Series are
    ordered by (date, exit_time), the project convention.
    """
    with csv_path.open() as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda r: (r["date"], r["exit_time"]))
    for r in rows:
        r["_d"] = date.fromisoformat(r["date"])
        r["_af"] = float(r["pnl_usd"]) - float(r["friction_usd"])
        r["_stop_pts"] = abs(float(r["entry_price"]) - float(r["stop"]))
        r["_units"] = int(float(r["contracts"]))

    # AF realized-equity curve: max drawdown + trough companions.
    equity = STARTING_BALANCE
    peak = STARTING_BALANCE
    af_max_dd = 0.0
    for r in rows:
        equity += r["_af"]
        peak = max(peak, equity)
        af_max_dd = max(af_max_dd, peak - equity)

    # Sub-period decay buckets (deep-dive definitions).
    buckets = []
    for name, lo, hi in SUBPERIODS:
        sel = [r for r in rows if lo <= r["_d"] <= hi]
        af = money(sum(r["_af"] for r in sel))
        buckets.append({"bucket": name,
                        "start": lo.isoformat(), "end": hi.isoformat(),
                        "trades": len(sel), "af_usd": af,
                        "green": af > 0})
    subperiods = {"buckets": buckets,
                  "green_count": sum(1 for b in buckets if b["green"]),
                  "bucket_count": len(buckets)}

    # Monthly green/red on AF sums.
    months: dict[str, dict] = {}
    for r in rows:
        key = r["date"][:7]
        m = months.setdefault(key, {"month": key, "trades": 0,
                                    "af_usd": 0.0})
        m["trades"] += 1
        m["af_usd"] += r["_af"]
    monthly = []
    for key in sorted(months):
        m = months[key]
        m["af_usd"] = money(m["af_usd"])
        m["green"] = m["af_usd"] > 0
        monthly.append(m)
    monthly_summary = {
        "months_with_trades": len(monthly),
        "green": sum(1 for m in monthly if m["green"]),
        "red": sum(1 for m in monthly if not m["green"]),
        "table": monthly,
    }

    # Long / short split.
    split = {}
    for side in ("long", "short"):
        sel = [r for r in rows if r["direction"] == side]
        wins = sum(1 for r in sel if float(r["pnl_usd"]) > 0)
        split[side] = {
            "trades": len(sel), "wins": wins,
            "losses": len(sel) - wins,
            "win_rate": round(wins / len(sel), 4) if sel else None,
            "gross_usd": money(sum(float(r["pnl_usd"]) for r in sel)),
            "af_usd": money(sum(r["_af"] for r in sel)),
            "af_share_of_total": None,  # filled by caller
        }

    # Stop-distance -> contract shape.
    stops = sorted(r["_stop_pts"] for r in rows)
    def pct(p: float) -> float:
        if not stops:
            return 0.0
        i = min(len(stops) - 1, int(p * len(stops)))
        return round(stops[i], 2)
    bins = [(0, 3), (3, 6), (6, 9), (9, 12), (12, 18), (18, 30),
            (30, float("inf"))]
    hist = []
    for lo, hi in bins:
        n = sum(1 for s in stops if lo < s <= hi) if lo else \
            sum(1 for s in stops if s <= hi)
        hist.append({"stop_pts": f"{lo:g}-{hi:g}" if hi != float("inf")
                     else f">{lo:g}", "trades": n})
    units_hist: dict[str, int] = {}
    for r in rows:
        units_hist[str(r["_units"])] = units_hist.get(str(r["_units"]), 0) + 1
    capped = sum(1 for r in rows if r["_units"] >= 5)
    risk_shape = {
        "stop_pts": {"min": round(stops[0], 2) if stops else None,
                     "p25": pct(0.25), "median": pct(0.5),
                     "mean": round(sum(stops) / len(stops), 2)
                     if stops else None,
                     "p75": pct(0.75),
                     "max": round(stops[-1], 2) if stops else None,
                     "histogram": hist},
        "contracts_histogram": dict(sorted(units_hist.items(),
                                           key=lambda kv: int(kv[0]))),
        "trades_at_5_contract_cap": capped,
        "avg_contracts": round(sum(r["_units"] for r in rows)
                               / len(rows), 2) if rows else None,
        "note": ("A trade sits at the 5-contract cap whenever its "
                 "stop is <= 6 ES points (150 / (6 x 5) = 5). Wider "
                 "stops scale down 4, 3, 2, 1 contracts; beyond "
                 "30 points the day is skipped-for-size and leaves "
                 "no row in this log."),
    }

    exits: dict[str, int] = {}
    for r in rows:
        exits[r["exit_reason"]] = exits.get(r["exit_reason"], 0) + 1

    return {"af_max_drawdown_usd": money(af_max_dd),
            "subperiods": subperiods,
            "monthly": monthly_summary,
            "long_short": split,
            "risk_shape": risk_shape,
            "exit_reasons": exits}


def main() -> int:
    lab = json.loads((RESULTS / "strategy_lab.json").read_text())
    lab_by_id = {r["id"]: r for r in lab["runs"]}
    df = load_csv(ROOT / "data" / ES_CSV)
    d = df["timestamp"].dt.date
    data_window = {"start": d.min().isoformat(),
                   "end": d.max().isoformat(),
                   "bars": int(len(df))}
    degraded_counts = {
        day: int((d == datetime.fromisoformat(day).date()).sum())
        for day in DEGRADED_DAYS}
    dq_note = (
        "Databento flagged the whole GLBX.MDP3 dataset for degraded "
        "quality on 2025-09-17, 2025-09-24 and 2025-11-28 (the same "
        "flags noted on the gold file and the MES cross-check); the "
        "ES file retains those days in full (bars in file: "
        + ", ".join(f"{k}: {v}" for k, v in degraded_counts.items())
        + ") — nothing dropped.")

    doc: dict = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "question": "Does the frozen ORB-15 strategy survive 24 "
                    "months on the MES futures tape — the only "
                    "faithful executable form at a $500 account — "
                    "judged by the same frozen criteria it passed "
                    "on SPY (Polygon SIP tape)?",
        "window": {"start": FULL_START.isoformat(),
                   "end": FULL_END.isoformat()},
        "data_file": f"data/{ES_CSV}",
        "data_window": data_window,
        "data_quality_note": dq_note,
        "spec": {
            "strategy": "orb15 — ORBStrategy(minutes=15) exactly as "
                        "frozen in ORB15_SPEC.md",
            "starting_balance_usd": STARTING_BALANCE,
            "risk_budget_usd": 150,
            "traded_as": "MES, $5/pt, tick 0.25, friction "
                         "$4.90/contract round trip",
            "sizing_rule": SIZING_RULE,
            "pass_test": "(a) 24m net-after-friction > 0 AND "
                         "(b) H2 (2025-10-21 -> 2026-10-19) net-"
                         "after-friction > 0 AND (c) after-friction "
                         "realized-equity trough > $0",
            "halves": {"h1": "2024-10-21 -> 2025-10-20",
                       "h2": "2025-10-21 -> 2026-10-19"},
        },
        "framing_note": FRAMING_NOTE,
    }

    # ---- STEP 1: slice verification --------------------------------
    scratch = Path("/tmp/orb15_mes_slice")
    scratch.mkdir(parents=True, exist_ok=True)
    sl = slice_check(df, lab_by_id, scratch)
    doc["slice_verification"] = sl
    if not sl["match"]:
        doc["status"] = ("ABORTED — slice verification failed on "
                         "the 24-month ES file; no 24-month numbers "
                         "produced. See slice_verification.")
        (RESULTS / "orb15_mes_validation.json").write_text(
            json.dumps(doc, indent=2))
        print("\nSLICE VERIFICATION FAILED — aborting.",
              file=sys.stderr)
        return 1
    print("Slice verification: orb15 MES matches the lab exactly "
          "on the 24-month file.\n")

    # ---- STEP 2: full 24-month run ---------------------------------
    rec = intraday_run("orb15", "mes", df, ES_CSV, ES_NOTE,
                       FULL_START, FULL_END, "orb15_mes_24mo", RESULTS)
    cell = {
        "id": "orb15_mes_24mo", "strategy": "orb15",
        "strategy_label": rec["strategy_label"],
        "symbol": rec["symbol"], "symbol_key": "mes",
        "data_file": f"data/{ES_CSV}", "data_symbol": ES_NOTE,
        "point_value_usd": rec["point_value_usd"],
        "friction_per_unit_usd": rec["friction_per_unit_usd"],
        "window": {"start": FULL_START.isoformat(),
                   "end": FULL_END.isoformat()},
        "trading_days": rec["trading_days"],
        "trades": rec["trades"], "wins": rec["wins"],
        "losses": rec["losses"], "win_rate": rec["win_rate"],
        "net_usd": rec["net_usd"], "net_r": rec["net_r"],
        "net_usd_after_friction": rec["net_usd_after_friction"],
        "ending_equity_usd": rec["ending_equity_usd"],
        "ending_equity_usd_after_friction":
            rec["ending_equity_usd_after_friction"],
        "max_drawdown_usd": rec["max_drawdown_usd"],
        "avg_units": rec["avg_units"],
        "skipped_for_size": rec["skipped_for_size"],
        "total_friction_usd": rec["total_friction_usd"],
        "trade_log": rec["trade_log"],
    }
    cell.update(enrich_from_csv(ROOT / cell["trade_log"],
                                rec["friction_per_unit_usd"]))
    cell["pass"] = pass_test(cell["net_usd_after_friction"],
                             cell["halves"],
                             cell["trough_after_friction_usd"])
    cell["verdict"] = "PASS" if cell["pass"]["passed"] else "FAIL"
    doc["cell"] = cell

    # ---- STEP 3: diagnostics + SPY side-by-side --------------------
    diag = analyze_trades(ROOT / cell["trade_log"],
                          rec["friction_per_unit_usd"])
    total_af = cell["net_usd_after_friction"]
    for side in ("long", "short"):
        s = diag["long_short"][side]
        s["af_share_of_total"] = (
            round(s["af_usd"] / total_af, 4)
            if total_af not in (0, None) else None)
    doc["diagnostics"] = diag

    spy = json.loads((RESULTS / "orb15_spy_poly.json").read_text())
    spy_enrich = enrich_from_csv(ROOT / "results" /
                                 "trades_orb15_spy_poly.csv", 0.04)
    n_mes, n_spy = cell["trades"], spy["trades"]
    doc["spy_comparison"] = {
        "spy_reference": {
            "source": "results/orb15_spy_poly.json (Polygon SIP "
                      "tape, arbiter run)",
            "trading_days": spy["trading_days"], "trades": n_spy,
            "wins": spy["wins"], "losses": spy["losses"],
            "win_rate": spy["win_rate"],
            "net_usd": spy["net_usd"], "net_r": spy["net_r"],
            "net_usd_after_friction": spy["net_usd_after_friction"],
            "gross_per_trade_usd": money(spy["net_usd"] / n_spy),
            "af_per_trade_usd":
                money(spy["net_usd_after_friction"] / n_spy),
            "trough_after_friction_usd":
                spy_enrich["trough_after_friction_usd"],
            "max_drawdown_usd": spy["max_drawdown_usd"],
            "skipped_for_size": spy["skipped_for_size"],
            "sizing": "uncapped shares = floor($150 / stop_$), "
                      "friction $0.04/share",
        },
        "mes": {
            "trading_days": cell["trading_days"], "trades": n_mes,
            "win_rate": cell["win_rate"],
            "net_usd": cell["net_usd"], "net_r": cell["net_r"],
            "net_usd_after_friction": total_af,
            "gross_per_trade_usd":
                money(cell["net_usd"] / n_mes) if n_mes else None,
            "af_per_trade_usd":
                money(total_af / n_mes) if n_mes else None,
            "trough_after_friction_usd":
                cell["trough_after_friction_usd"],
            "max_drawdown_usd": cell["max_drawdown_usd"],
            "skipped_for_size": cell["skipped_for_size"],
        },
        "af_per_trade_ratio_mes_over_spy": (
            round((total_af / n_mes)
                  / (spy["net_usd_after_friction"] / n_spy), 4)
            if n_mes and total_af else None),
        "note": ("Per-trade AF edge is the cleanest cross-tape "
                 "comparison: both runs risk the same $150 budget "
                 "per trade, so the ratio isolates the tape effect "
                 "(overnight-continuation open vs cash auction "
                 "open) from sizing. Totals also differ mechanically "
                 "— MES caps at 5 contracts and skips wide-stop "
                 "days the uncapped SPY run takes, and ES has "
                 "session days the NYSE calendar does not."),
    }
    doc["status"] = "COMPLETE"
    (RESULTS / "orb15_mes_validation.json").write_text(
        json.dumps(doc, indent=2))

    p = cell["pass"]
    print("\n=== ORB-15 ON MES — 24-MONTH VALIDATION ===")
    print(f"{cell['id']:18s} {cell['trades']:4d}t "
          f"({cell['wins']}W/{cell['losses']}L) "
          f"WR {cell['win_rate']} net ${cell['net_usd']:+10.2f} "
          f"({cell['net_r']:+.2f}R) AF "
          f"${cell['net_usd_after_friction']:+10.2f} "
          f"H1 ${cell['halves']['h1']['net_after_friction_usd']:+9.2f} "
          f"H2 ${cell['halves']['h2']['net_after_friction_usd']:+9.2f} "
          f"trough ${cell['trough_after_friction_usd']:9.2f} "
          f"skipped {cell['skipped_for_size']} -> {cell['verdict']}")
    print("Sub-periods (AF): " + " · ".join(
        f"{b['bucket']} ${b['af_usd']:+.2f}"
        for b in diag["subperiods"]["buckets"]))
    cmp_ = doc["spy_comparison"]
    print(f"Per-trade AF edge: MES ${cmp_['mes']['af_per_trade_usd']:+.2f} "
          f"vs SPY ${cmp_['spy_reference']['af_per_trade_usd']:+.2f} "
          f"(ratio {cmp_['af_per_trade_ratio_mes_over_spy']})")
    print(f"\nWrote {RESULTS / 'orb15_mes_validation.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
