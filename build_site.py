#!/usr/bin/env python3
"""Regenerate site/index.html from results/summary.json + trade logs.

Static, self-contained dashboard (data embedded inline, no external
assets). Re-run after run_matrix.py to refresh the page:
    python3 build_site.py
"""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Tradovate integration status — update as the integration progresses.
BROKER_STATUS = (
    "Integration layer built against the Tradovate DEMO API and covered by "
    "mocked unit tests (auth, bar parsing, bracket payloads, kill-switch "
    "refusal). Live trading is disabled by a hard-coded kill switch "
    "(LIVE_TRADING_ENABLED = False). Blocked on: Matt's Tradovate account "
    "+ demo API key."
)

MATT_CHECKLIST = [
    "Open a Tradovate account (tradovate.com) — the FCM is NinjaTrader Clearing.",
    "Fund it only when ready later; the API Access add-on needs a live funded account (min $1,000) — but demo work can start as soon as the account exists.",
    "Subscribe to the API Access add-on ($25/mo) in Application Settings → API Access, then generate an API key (you'll get a CID + secret).",
    "Create the API key for the DEMO environment first — we validate everything in simulation.",
    "Hand the credentials over via secure entry (password manager / vault), NEVER by pasting them in chat.",
    "Live trading is a separate, later decision that needs Matt's explicit go — the bot cannot trade live by construction until the kill switch is deliberately flipped.",
]


def money(v) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def signed_money(v) -> str:
    if v is None:
        return "—"
    return ("+" if v >= 0 else "-") + f"${abs(v):,.2f}"


def equity_svg(curves: list[tuple[str, list[dict], str]], width=720, height=230) -> str:
    """Inline SVG line chart of one or more equity curves."""
    all_eq = [p["equity"] for _, pts, _ in curves for p in pts]
    if not all_eq:
        return "<p class='muted'>No equity data.</p>"
    lo, hi = min(all_eq), max(all_eq)
    pad = max((hi - lo) * 0.12, 5.0)
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 58, 12, 12, 26
    iw, ih = width - left - right, height - top - bottom

    def xy(i, n, eq):
        x = left + (iw * i / max(n - 1, 1))
        y = top + ih * (1 - (eq - lo) / (hi - lo))
        return f"{x:.1f},{y:.1f}"

    parts = [
        f"<svg viewBox='0 0 {width} {height}' class='chart' role='img' "
        f"aria-label='Equity curve'>"
    ]
    # $500 baseline
    if lo < 500 < hi:
        y = top + ih * (1 - (500 - lo) / (hi - lo))
        parts.append(
            f"<line x1='{left}' y1='{y:.1f}' x2='{width - right}' y2='{y:.1f}' "
            "stroke='#5b6b82' stroke-dasharray='4 4' stroke-width='1'/>"
            f"<text x='{left - 4}' y='{y + 3:.1f}' class='ax' text-anchor='end'>$500 start</text>"
        )
    parts.append(
        f"<text x='{left - 4}' y='{top + 4}' class='ax' text-anchor='end'>{money(hi - pad)}</text>"
        f"<text x='{left - 4}' y='{top + ih}' class='ax' text-anchor='end'>{money(lo + pad)}</text>"
    )
    for label, pts, color in curves:
        if len(pts) < 2:
            continue
        points = " ".join(xy(i, len(pts), p["equity"]) for i, p in enumerate(pts))
        parts.append(
            f"<polyline points='{points}' fill='none' stroke='{color}' stroke-width='2'/>"
        )
        lx, ly = xy(len(pts) - 1, len(pts), pts[-1]["equity"]).split(",")
        parts.append(
            f"<circle cx='{lx}' cy='{ly}' r='3' fill='{color}'/>"
            f"<text x='{float(lx) - 4:.1f}' y='{float(ly) - 8:.1f}' class='ax' "
            f"text-anchor='end' fill='{color}'>{label} {money(pts[-1]['equity'])}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def stat_cards(run: dict, title: str) -> str:
    cards = [
        ("Trades", f"{run['trades']} <span class='sub'>({run['wins']}W / {run['losses']}L)</span>"),
        ("Win rate", pct(run["win_rate"])),
        ("Net P&L", f"<span class='{'pos' if run['net_usd'] >= 0 else 'neg'}'>{signed_money(run['net_usd'])}</span>"),
        ("Net R", f"{run['net_r']:+.2f}R"),
        ("Ending equity", money(run["ending_equity_usd"])),
        ("Max drawdown", money(run["max_drawdown_usd"])),
        ("Avg contracts", "—" if run["avg_contracts"] is None else f"{run['avg_contracts']:.1f}"),
        ("Skipped (size)", str(run["skipped_for_size"])),
    ]
    body = "".join(
        f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
        for k, v in cards
    )
    rng = run["date_range"]
    return (
        f"<h2>{html.escape(title)}</h2>"
        f"<p class='muted'>{html.escape(run['data_symbol'])} · {run['bar']} bars · "
        f"{rng['start']} → {rng['end']} · {run['trading_days']} trading days</p>"
        f"<div class='grid'>{body}</div>"
    )


def trade_table(run: dict) -> str:
    path = ROOT / run["trade_log"]
    if not path.exists():
        return "<p class='muted'>Trade log not found.</p>"
    with path.open() as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return "<p class='muted'>No trades in this run.</p>"
    out = [
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Date</th><th>Dir</th><th>Contracts</th><th>Entry</th><th>Stop</th>"
        "<th>Target</th><th>Exit</th><th>Exit reason</th><th>P&amp;L</th><th>R</th>"
        "</tr></thead><tbody>"
    ]
    for r in rows:
        pnl = float(r["pnl_usd"])
        cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "")
        out.append(
            "<tr>"
            f"<td>{r['date']}</td><td>{r['direction']}</td><td>{r['contracts']}</td>"
            f"<td>{float(r['entry_price']):,.2f}<span class='sub'> {r['entry_time']}</span></td>"
            f"<td>{float(r['stop']):,.2f}</td><td>{float(r['target']):,.2f}</td>"
            f"<td>{float(r['exit_price']):,.2f}<span class='sub'> {r['exit_time']}</span></td>"
            f"<td>{r['exit_reason']}</td>"
            f"<td class='{cls}'>{signed_money(pnl)}</td>"
            f"<td class='{cls}'>{float(r['r_multiple']):+.2f}R</td>"
            "</tr>"
        )
    out.append("</tbody></table></div>")
    return "".join(out)


def experiments_section() -> str:
    """Experiments matrix (run_experiments.py) — 6 variants x 5 symbols.

    Reads results/experiments.json; returns "" when the matrix has not
    been run so the rest of the dashboard still builds.
    """
    path = ROOT / "results" / "experiments.json"
    if not path.exists():
        return ""
    matrix = json.loads(path.read_text())
    runs = matrix["runs"]
    pos = [r for r in runs if r["net_usd"] > 0]
    by_id = {r["id"]: r for r in runs}
    mgc_pos = sum(
        1 for r in runs if r["symbol"] == "MGC" and r["net_usd"] > 0
    )
    rows_html = []
    for r in runs:
        cls = "pos" if r["net_usd"] > 0 else "neg"
        hl = " style='background:#12241a'" if r["net_usd"] > 0 else ""
        rows_html.append(
            f"<tr{hl}>"
            f"<td>{html.escape(r['label'])}</td>"
            f"<td>{r['trading_days']}</td><td>{r['trades']}</td>"
            f"<td>{r['wins']}W / {r['losses']}L</td>"
            f"<td>{pct(r['win_rate'])}</td>"
            f"<td class='{cls}'>{signed_money(r['net_usd'])}</td>"
            f"<td>{r['net_r']:+.2f}R</td>"
            f"<td>{money(r['ending_equity_usd'])}</td>"
            f"<td>{money(r['max_drawdown_usd'])}</td>"
            f"<td>{'—' if r['avg_units'] is None else r['avg_units']}</td>"
            f"<td>{r['skipped_for_size']}</td></tr>"
        )
    best = max(runs, key=lambda r: r["net_usd"])
    interp = (
        f"<p><b>Honest reading.</b> {len(pos)} of 30 cells finished positive, and every "
        f"variant still lost money summed across the five symbols — no adjustment turned "
        f"the First Candle Rule into a broadly profitable system. The one consistent bright "
        f"spot is gold (MGC): positive in {mgc_pos} of its 6 cells, including the untouched "
        f"baseline ({signed_money(by_id['mgc_1m_v0_baseline']['net_usd'])}, "
        f"{by_id['mgc_1m_v0_baseline']['net_r']:+.2f}R), the single best cell in the matrix. "
        f"Partials (V3) reliably softened losses — higher win rates, shallower drawdowns — "
        f"without creating an edge. Min-FVG (V1) backfired on MNQ "
        f"({signed_money(by_id['mnq_1m_v1_min_fvg']['net_usd'])}): rejecting a setup does not "
        f"end the day, it just hands the day to a later, worse setup. SPY and MNQ — the "
        f"video's own market — lost under every variant.</p>"
        f"<p><b>Small-sample warning.</b> Each cell holds only ~8–23 trades over ~60 days "
        f"(roughly 13 in a typical run). At that size a few trades' difference flips almost "
        f"any cell's sign — gold's entire baseline profit is about one extra winning trade — "
        f"and picking the best of 30 cells is itself a form of overfitting: with 30 tries, "
        f"chance alone manufactures a few good-looking cells. Nothing here is a validated "
        f"edge; the gold column is a hypothesis for a longer test, not a result.</p>"
    )
    return (
        "<h2>Experiments — can adjustments make it profitable?</h2>"
        "<p class='muted'>Same 60-day 1-minute Databento window (2026-08-10 → 2026-10-20; "
        "SPY → 2026-10-19), same $500 account / $150 risk / ≤5 contracts spec on every cell. "
        "Futures priced off the full-size contract tape, sized as the micro. Variants: "
        "V0 baseline · V1 FVG ≥ 0.5×ATR(14) · V2 skip wide-first-candle days · "
        "V3 bank half at +1.5R + breakeven stop · V4 first-candle-body trend filter · "
        "V5 all four combined. Full matrix: results/experiments.json; "
        "per-run trade logs in results/.</p>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Run</th><th>Days</th><th>Trades</th><th>W/L</th><th>Win rate</th>"
        "<th>Net $</th><th>Net R</th><th>End equity</th><th>Max DD</th>"
        "<th>Avg size</th><th>Skipped</th>"
        f"</tr></thead><tbody>{''.join(rows_html)}</tbody></table></div>"
        f"<div class='panel'>{interp}</div>"
    )


def gold24_section() -> str:
    """Gold 24-month validation (run_gold24.py) + gold bot status.

    Reads results/gold24.json; returns "" when the validation has not
    been run so the rest of the dashboard still builds.
    """
    path = ROOT / "results" / "gold24.json"
    if not path.exists():
        return ""
    doc = json.loads(path.read_text())
    runs = doc["runs"]
    by_variant = {r["variant"]: r for r in runs}
    dq = doc["data_quality"]

    def checks_cell(r) -> str:
        c = r["pass_checks"]
        mark = {True: "✓", False: "✗"}
        return (
            f"<span class='{'pos' if r['passed'] else 'neg'}'>"
            f"{'PASS' if r['passed'] else 'FAIL'}</span><br>"
            f"<span class='sub'>{mark[c['a_full_run_positive']]} full run · "
            f"{mark[c['b_second_half_positive']]} 2nd half · "
            f"{mark[c['c_trough_above_zero']]} trough &gt; $0</span>"
        )

    rows_html = []
    for r in runs:
        cls = "pos" if r["net_usd"] > 0 else "neg"
        hl = " style='background:#12241a'" if r["passed"] else ""
        rows_html.append(
            f"<tr{hl}>"
            f"<td>{html.escape(r['label'])}</td>"
            f"<td>{r['trading_days']}</td><td>{r['trades']}</td>"
            f"<td>{r['wins']}W / {r['losses']}L</td>"
            f"<td>{pct(r['win_rate'])}</td>"
            f"<td class='{cls}'>{signed_money(r['net_usd'])}</td>"
            f"<td>{r['net_r']:+.2f}R</td>"
            f"<td>{money(r['ending_equity_usd'])}</td>"
            f"<td>{money(r['max_drawdown_usd'])}</td>"
            f"<td>{money(r['equity_trough_usd'])}<span class='sub'> {html.escape(str(r['equity_trough_date']))}</span></td>"
            f"<td>{r['longest_losing_streak']}</td>"
            f"<td>{r['skipped_for_size']}</td>"
            f"<td>{checks_cell(r)}</td></tr>"
        )

    half_rows = []
    for r in runs:
        for name, half in (("1st half", r["halves"]["first"]),
                           ("2nd half", r["halves"]["second"])):
            cls = "pos" if half["net_usd"] > 0 else "neg"
            half_rows.append(
                f"<tr><td>{html.escape(r['variant_label'])} — {name} "
                f"<span class='sub'>{html.escape(half['range'])}</span></td>"
                f"<td>{half['trading_days']}</td><td>{half['trades']}</td>"
                f"<td>{half['wins']}W / {half['losses']}L</td>"
                f"<td>{pct(half['win_rate'])}</td>"
                f"<td class='{cls}'>{signed_money(half['net_usd'])}</td>"
                f"<td>{half['net_r']:+.2f}R</td>"
                f"<td>{money(half['ending_equity_usd'])}</td>"
                f"<td>{money(half['max_drawdown_usd'])}</td>"
                f"<td>{money(half['equity_trough_usd'])}</td></tr>"
            )

    winner = by_variant["v0"] if doc["winner"] else None
    chart = ""
    if winner is not None:
        chart = (
            "<div class='panel'><h2 style='margin-top:0'>Equity curve — "
            "gold V0 baseline, 24 months (start $500)</h2>"
            + equity_svg([("MGC V0", winner["equity_curve"], "#f5c451")])
            + "</div>"
        )

    short_list = ", ".join(
        f"{d['date']} ({d['rth_bars']})" for d in dq["short_days"]
    )
    flagged = ", ".join(dq["databento_degraded_flag_dates"])
    interp = (
        f"<p><b>Verdict.</b> The untouched baseline (V0) passed all three frozen "
        f"criteria — full run positive ({signed_money(by_variant['v0']['net_usd'])}, "
        f"{by_variant['v0']['net_r']:+.2f}R over {by_variant['v0']['trades']} trades), "
        f"second half positive ({signed_money(by_variant['v0']['halves']['second']['net_usd'])}, "
        f"{by_variant['v0']['halves']['second']['net_r']:+.2f}R), and the equity trough "
        f"({money(by_variant['v0']['equity_trough_usd'])} on "
        f"{html.escape(str(by_variant['v0']['equity_trough_date']))}) never came close to "
        f"zeroing the $500 account. V5 (combined) also passed but is checked after V0 in "
        f"the frozen priority order. V3 (partials) made the most dollars "
        f"({signed_money(by_variant['v3']['net_usd'])}) yet FAILED criterion (b): its "
        f"second-half net R was {by_variant['v3']['halves']['second']['net_r']:+.2f}R — "
        f"banking halves made its recent trades smaller-but-losing in R terms. The "
        f"criteria were frozen before the run and applied mechanically, so the bot runs "
        f"the plain baseline.</p>"
        f"<p><b>Honest caveats.</b> 224 trades is a real sample — this is not the "
        f"13-trade noise of the 60-day cells — and the 28.6% win rate sits above the "
        f"25% breakeven a 3:1 target needs. But the second half was much weaker than "
        f"the first (+2.06R of the +26.26R total), the max drawdown ($1,543) is three "
        f"times the starting account, and <b>no commissions or slippage are modeled</b>: "
        f"224 round trips of micro commissions plus a tick or two of slippage per side "
        f"would take a real bite out of the headline number. This is a hypothesis that "
        f"survived a 24-month test, not a promise.</p>"
        f"<p><b>Data quality.</b> {dq['rth_trading_days']} RTH days; "
        f"{len(dq['short_days'])} days had fewer than {dq['short_day_threshold']} RTH "
        f"bars (a full session is {dq['full_rth_bars']}): {html.escape(short_list)}. "
        f"Most are scheduled early closes / holiday sessions. Databento separately "
        f"flagged {html.escape(flagged)} as degraded — none of those three is a "
        f"short-bar day in this file, so the degradation did not remove RTH bars. "
        f"Short days were not dropped; the engine ran whatever bars exist.</p>"
    )
    bot_panel = (
        "<div class='panel'><h2 style='margin-top:0'>Gold bot — built (demo only, not deployed)</h2>"
        "<p><b>gold_bot.py</b> runs the winning V0 baseline on MGC through the existing "
        "Tradovate DEMO layer: resolves the front-month contract each morning, feeds "
        "1-minute bars to the same engine the backtester uses, places the entry as a "
        "market order with the engine's stop/target attached as an OSO bracket the "
        "moment a setup confirms, and liquidates anything still open at 15:55 ET. "
        "One trade per day. The config (<b>config_gold.yaml</b>) is frozen — the "
        "runner refuses to start if the symbol, sizing, or variant flags drift from "
        "the validated spec, if the kill switch is not in its off position, or if "
        "the client is not on the demo host.</p>"
        "<p><b>Status: NOT deployed anywhere. It has never connected to the real "
        "Tradovate API and has never placed an order</b> — it is covered by mocked "
        "unit tests only, because no Tradovate account or demo API key exists yet. "
        "Matt still needs to: open + fund a Tradovate account (API Access needs a "
        "live funded account, min $1,000), subscribe to API Access ($25/mo), "
        "generate a DEMO API key, and provide the credentials via secure entry "
        "(env vars in .env.example) — never in chat. Live trading remains a "
        "separate decision behind the hard-coded kill switch.</p></div>"
    )
    return (
        "<h2>Gold — 24-month validation</h2>"
        "<p class='muted'>The one hypothesis from the matrix above, tested properly: "
        "GC.v.0 1-minute bars (Databento GLBX.MDP3), 2024-10-21 → 2026-10-20, traded "
        "as MGC ($10/pt), same $500 / $150-risk / ≤5-contract spec, frozen variant "
        "definitions — no retuning. Pass criteria, frozen before the run and checked "
        "in the order V0 → V3 → V5: (a) 24-month net $ and net R both positive, "
        "(b) second half also net positive on both, (c) equity trough above $0. "
        "Full record: results/gold24.json.</p>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Variant</th><th>Days</th><th>Trades</th><th>W/L</th><th>Win rate</th>"
        "<th>Net $</th><th>Net R</th><th>End equity</th><th>Max DD</th>"
        "<th>Trough</th><th>Worst streak</th><th>Skipped</th><th>Verdict</th>"
        f"</tr></thead><tbody>{''.join(rows_html)}</tbody></table></div>"
        f"{chart}"
        "<h2>Gold — half splits</h2>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Variant — half</th><th>Days</th><th>Trades</th><th>W/L</th><th>Win rate</th>"
        "<th>Net $</th><th>Net R</th><th>End equity</th><th>Max DD</th><th>Trough</th>"
        f"</tr></thead><tbody>{''.join(half_rows)}</tbody></table></div>"
        f"<div class='panel'>{interp}</div>"
        f"{bot_panel}"
    )


def validation24_section() -> str:
    """Winners — 24-month validation (run_validation24.py +
    run_validation24_spy.py).

    Reads results/validation24.json; returns "" when the validation
    has not been completed so the rest of the dashboard still builds.
    """
    path = ROOT / "results" / "validation24.json"
    if not path.exists():
        return ""
    doc = json.loads(path.read_text())
    if "cross_sample_replication" not in doc:
        return ""  # gated/partial record — nothing honest to show yet
    cells = {c["id"]: c for c in doc["cells"]}
    pd = cells["pd_sweep_mgc_24mo"]
    spy_ids = ("ib_break_spy_24mo", "fcr_v0_spy_24mo",
               "vwap_trend_spy_24mo", "orb15_spy_24mo")
    spy_cells = [cells[i] for i in spy_ids]
    pf = doc["portfolio"]
    pfc = pf["combined"]

    def af_cls(v) -> str:
        return "pos" if v > 0 else "neg"

    def verdict_cell(c) -> str:
        v = c["verdict"]
        cls = "pos" if v == "PASS" else ("neg" if v == "FAIL" else "")
        style = " style='color:#f5c451'" if v.startswith("PASS (") else ""
        return f"<span class='{cls}'{style}><b>{html.escape(v)}</b></span>"

    repl_rows = []
    for r in doc["cross_sample_replication"]["cells"]:
        lf, nf = r["lab_file"], r["new_file"]
        stab = r["stability"]
        scls = "pos" if stab == "STABLE" else "neg"
        repl_rows.append(
            f"<tr><td>{html.escape(r['cell'])}</td>"
            f"<td>{lf['trades']}</td>"
            f"<td class='{af_cls(lf['net_usd'])}'>{signed_money(lf['net_usd'])}</td>"
            f"<td class='{af_cls(lf['net_after_friction'])}'>{signed_money(lf['net_after_friction'])}</td>"
            f"<td>{nf['trades']}</td>"
            f"<td class='{af_cls(nf['net_usd'])}'>{signed_money(nf['net_usd'])}</td>"
            f"<td class='{af_cls(nf['net_after_friction'])}'>{signed_money(nf['net_after_friction'])}</td>"
            f"<td class='{scls}'><b>{stab}</b></td></tr>"
        )

    run_rows = []
    for c in spy_cells:
        h = c["halves"]
        run_rows.append(
            f"<tr><td>{html.escape(c['id'].replace('_24mo', ''))}</td>"
            f"<td>{c['trading_days']}</td><td>{c['trades']}</td>"
            f"<td>{c['wins']}W / {c['losses']}L</td>"
            f"<td>{pct(c['win_rate'])}</td>"
            f"<td class='{af_cls(c['net_usd'])}'>{signed_money(c['net_usd'])}</td>"
            f"<td>{c['net_r']:+.2f}R</td>"
            f"<td class='{af_cls(c['net_usd_after_friction'])}'><b>{signed_money(c['net_usd_after_friction'])}</b></td>"
            f"<td>{money(c['ending_equity_usd_after_friction'])}</td>"
            f"<td>{money(c['max_drawdown_usd'])}</td>"
            f"<td class='{af_cls(h['h1']['net_after_friction_usd'])}'>{signed_money(h['h1']['net_after_friction_usd'])}</td>"
            f"<td class='{af_cls(h['h2']['net_after_friction_usd'])}'>{signed_money(h['h2']['net_after_friction_usd'])}</td>"
            f"<td class='{af_cls(c['trough_after_friction_usd'])}'>{money(c['trough_after_friction_usd'])}"
            f"<span class='sub'> {html.escape(str(c['trough_after_friction_at']))}</span></td>"
            f"<td>{c['skipped_for_size']}</td>"
            f"<td>{html.escape(c['cross_sample_stability'])}</td>"
            f"<td>{verdict_cell(c)}</td></tr>"
        )

    pd_h = pd["halves"]
    pd_panel = (
        "<div class='panel'><h2 style='margin-top:0'>Previous-day sweep — MGC (gold): "
        "<span class='neg'>FAIL</span></h2>"
        f"<p>Run on the SAME futures file as the lab (GLBX.MDP3 is deterministic — "
        f"the lab window reproduces to the cent), full 24 months: {pd['trades']} trades "
        f"({pd['wins']}W / {pd['losses']}L, {pct(pd['win_rate'])} win rate), gross "
        f"{signed_money(pd['net_usd'])} ({pd['net_r']:+.2f}R) — but "
        f"<b>{signed_money(pd['net_usd_after_friction'])} after friction</b>. "
        f"Half splits after friction: H1 {signed_money(pd_h['h1']['net_after_friction_usd'])}, "
        f"H2 {signed_money(pd_h['h2']['net_after_friction_usd'])}. The after-friction equity "
        f"trough was {money(pd['trough_after_friction_usd'])} on "
        f"{html.escape(str(pd['trough_after_friction_at']))} — a real $500 account would "
        f"have been wiped out long before the second half recovered. Fails pass criteria "
        f"(a) and (c). The lab's +$726.20 over 120 days was a favourable window, not an edge.</p></div>"
    )

    integrity_panel = (
        "<div class='panel'><h2 style='margin-top:0'>⚠ SPY data-integrity finding</h2>"
        "<p><b>The SPY 1-minute feed is not reproducible.</b> Two separate downloads of "
        "the same SPY history from Databento's DBEQ.BASIC dataset disagree with each "
        "other: across the 84 overlapping days they share, 10,234 of 37,015 bars "
        "(about a third) differ in price or volume, and a third download disagrees "
        "with both. There is no single 'true' SPY tape in this data — each download "
        "is its own sample of a sampled feed. The futures data (GLBX.MDP3) does NOT "
        "have this problem: the gold file used here is bit-for-bit the lab's file "
        "and reproduces results to the cent.</p>"
        "<p>Consequences, stated plainly: (1) every SPY intraday number in the "
        "Strategy Lab above is a property of the one download that produced it — "
        "single-sample evidence, now labelled as such in the README; (2) the SPY "
        "validation below was run end-to-end on ONE file (spy_1m_24mo.csv, "
        "2024-10-21 → 2026-10-19) so it is at least internally consistent; "
        "(3) each verdict is printed next to a cross-sample stability check — "
        "whether the cell kept its after-friction sign when the identical lab "
        "window was re-run on the other download. Two of the four SPY winners "
        "flipped sign across downloads. Treat their verdicts accordingly.</p></div>"
    )

    pf_panel = (
        "<div class='panel'><h2 style='margin-top:0'>SPY portfolio, 24 months "
        f"— <span class='{'pos' if pf['pass']['passed'] else 'neg'}'>"
        f"{'PASS' if pf['pass']['passed'] else 'FAIL'}</span></h2>"
        f"<p>The lab's three SPY components (IB breakout + FCR V0 + VWAP trend) in "
        f"one shared $500 account on the new file, $300 open-risk cap: "
        f"{pfc['trades_taken']} of {pfc['trades_considered']} trades taken "
        f"({pfc['trades_skipped_by_cap']} skipped by the cap), combined "
        f"<b>{signed_money(pfc['net_usd'])}</b> after friction, "
        f"{pct(pfc['win_rate'])} win rate ({pfc['wins']}W / {pfc['losses']}L), "
        f"ending equity {money(pfc['ending_equity_usd'])}, realized-curve max "
        f"drawdown {money(pfc['max_drawdown_usd'])}, trough "
        f"{money(pfc['trough_usd'])} (never below the $500 start on the realized "
        f"curve). Naive uncapped sum of the components: "
        f"{signed_money(pfc['naive_uncapped_sum_usd'])}. Caveats: the curve is "
        f"realized-P&amp;L only (no mark-to-market), one component (VWAP trend) "
        f"individually FAILS the trough test standalone, and the whole portfolio "
        f"inherits the single-sample SPY data caveat above.</p></div>"
    )

    return (
        "<h2>Winners — 24-month validation</h2>"
        "<p class='muted'>The lab's top five cells re-run over 24 months "
        "(2024-10-21 → 2026-10-20; SPY file ends 2026-10-19), same frozen spec, "
        "same friction model. Pass test, frozen before the runs: (a) 24-month "
        "net-after-friction &gt; $0, (b) second half (2025-10-21 → end) "
        "net-after-friction &gt; $0, (c) after-friction realized-equity trough "
        "&gt; $0. Full record: results/validation24.json.</p>"
        f"{pd_panel}"
        f"{integrity_panel}"
        "<h2>Cross-sample replication — lab window (2026-06-22 → end), both SPY downloads</h2>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Cell</th><th>Lab file trades</th><th>Gross</th><th>After friction</th>"
        "<th>New file trades</th><th>Gross</th><th>After friction</th><th>Stability</th>"
        f"</tr></thead><tbody>{''.join(repl_rows)}</tbody></table></div>"
        "<p class='muted'>STABLE = the after-friction sign agrees across the two "
        "downloads; FLIPPED = it does not. The engines reproduce the lab exactly "
        "on the lab file — the disagreement is in the data, not the code.</p>"
        "<h2>SPY — full 24 months on the single new file</h2>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Cell</th><th>Days</th><th>Trades</th><th>W/L</th><th>Win rate</th>"
        "<th>Gross $</th><th>Net R</th><th>After friction</th><th>End equity (AF)</th>"
        "<th>Max DD</th><th>H1 AF</th><th>H2 AF</th><th>Trough (AF)</th>"
        "<th>Skipped</th><th>Stability</th><th>Verdict</th>"
        f"</tr></thead><tbody>{''.join(run_rows)}</tbody></table></div>"
        f"{pf_panel}"
    )


def crosscheck_mes_section() -> str:
    """MES cross-check (run_crosscheck_mes.py).

    The two credible SPY winners re-run on MES (ES.v.0 tape,
    GLBX.MDP3 — deterministic) over 24 months. Reads
    results/crosscheck_mes.json; returns "" when the cross-check
    has not been run so the rest of the dashboard still builds.
    """
    path = ROOT / "results" / "crosscheck_mes.json"
    if not path.exists():
        return ""
    doc = json.loads(path.read_text())
    if doc.get("status") != "COMPLETE":
        return ""  # aborted/partial record — nothing honest to show
    cells = doc["cells"]
    slices = {s["cell"]: s for s in doc["slice_verification"]}
    pf = doc["portfolio"]

    def af_cls(v) -> str:
        return "pos" if v > 0 else "neg"

    def crit_marks(c) -> str:
        p = c["pass"]
        marks = []
        for key, label in (
                ("a_net_after_friction_positive", "a"),
                ("b_h2_net_after_friction_positive", "b"),
                ("c_trough_above_zero", "c")):
            marks.append(f"{label} {'✓' if p[key] else '✗'}")
        return " · ".join(marks)

    slice_rows = []
    for s in doc["slice_verification"]:
        e, g = s["expected"], s["got"]
        slice_rows.append(
            f"<tr><td>{html.escape(s['cell'])}</td>"
            f"<td>{e['trades']}</td>"
            f"<td>{signed_money(e['net_usd'])}</td>"
            f"<td>{signed_money(e['net_after_friction'])}</td>"
            f"<td>{g['trades']}</td>"
            f"<td>{signed_money(g['net_usd'])}</td>"
            f"<td>{signed_money(g['net_after_friction'])}</td>"
            f"<td class='pos'><b>{'MATCH' if s['match'] else 'MISMATCH'}</b></td></tr>"
        )

    run_rows = []
    for c in cells:
        h = c["halves"]
        v = c["verdict"]
        vcls = "pos" if v == "PASS" else "neg"
        friction_total = c["net_usd"] - c["net_usd_after_friction"]
        run_rows.append(
            f"<tr><td>{html.escape(c['id'].replace('_24mo', ''))}</td>"
            f"<td>{c['trading_days']}</td><td>{c['trades']}</td>"
            f"<td>{c['wins']}W / {c['losses']}L</td>"
            f"<td>{pct(c['win_rate'])}</td>"
            f"<td class='{af_cls(c['net_usd'])}'>{signed_money(c['net_usd'])}</td>"
            f"<td>{c['net_r']:+.2f}R</td>"
            f"<td class='{af_cls(c['net_usd_after_friction'])}'><b>{signed_money(c['net_usd_after_friction'])}</b></td>"
            f"<td>{money(c['ending_equity_usd_after_friction'])}</td>"
            f"<td>{money(c['max_drawdown_usd'])}</td>"
            f"<td class='{af_cls(h['h1']['net_after_friction_usd'])}'>{signed_money(h['h1']['net_after_friction_usd'])}</td>"
            f"<td class='{af_cls(h['h2']['net_after_friction_usd'])}'>{signed_money(h['h2']['net_after_friction_usd'])}</td>"
            f"<td class='{af_cls(c['trough_after_friction_usd'])}'>{money(c['trough_after_friction_usd'])}"
            f"<span class='sub'> {html.escape(str(c['trough_after_friction_at']))}</span></td>"
            f"<td>{c['skipped_for_size']}</td>"
            f"<td>{crit_marks(c)}</td>"
            f"<td class='{vcls}'><b>{v}</b></td></tr>"
            f"<tr><td colspan='16' class='sub'>Friction paid over the "
            f"run: {money(friction_total)} "
            f"({c['friction_per_unit_usd']:.2f}/contract round trip) — "
            f"gross edge vs friction is the whole story of this cell."
            f"</td></tr>"
        )

    if pf.get("built"):
        pfc = pf["combined"]
        pf_panel = (
            "<div class='panel'><h2 style='margin-top:0'>MES mini-portfolio, "
            "24 months — "
            f"<span class='{'pos' if pf['pass']['passed'] else 'neg'}'>"
            f"{'PASS' if pf['pass']['passed'] else 'FAIL'}</span></h2>"
            f"<p>IB breakout + VWAP trend on MES in one shared $500 account, "
            f"$300 open-risk cap: {pfc['trades_taken']} of "
            f"{pfc['trades_considered']} trades taken "
            f"({pfc['trades_skipped_by_cap']} skipped by the cap), combined "
            f"<b>{signed_money(pfc['net_usd'])}</b> after friction, "
            f"{pct(pfc['win_rate'])} win rate, ending equity "
            f"{money(pfc['ending_equity_usd'])}, realized-curve max drawdown "
            f"{money(pfc['max_drawdown_usd'])}, trough "
            f"{money(pfc['trough_usd'])}. Naive uncapped sum: "
            f"{signed_money(pfc['naive_uncapped_sum_usd'])}. Realized-P&amp;L "
            "curve only (no mark-to-market).</p></div>"
        )
    else:
        pf_panel = (
            "<div class='panel'><h2 style='margin-top:0'>MES mini-portfolio "
            "— not built</h2>"
            f"<p>{html.escape(pf['reason'])} A conjunction is only "
            "meaningful once both components clear the frozen bar on "
            "their own; combining two failing cells would manufacture "
            "a number, not test one.</p></div>"
        )

    return (
        "<h2>MES cross-check — the SPY survivors on deterministic "
        "futures data</h2>"
        "<p class='muted'>The validation above leaves two SPY cells worth "
        "believing — IB breakout (PASS, STABLE across downloads) and VWAP "
        "trend (STABLE, failed only the trough test) — but both rest on the "
        "non-reproducible DBEQ.BASIC tape. This cross-check re-runs the same "
        "two strategies, same frozen spec and friction model, on MES "
        "(ES.v.0 tape from GLBX.MDP3, which IS deterministic) over the same "
        "24 months (2024-10-21 → 2026-10-20), traded as MES at $5/pt with "
        "$4.90/contract round-trip friction. Full record: "
        "results/crosscheck_mes.json (harness: run_crosscheck_mes.py).</p>"
        "<h3>Slice verification — lab window (2026-06-22 → 2026-10-20) "
        "on the new 24-month ES file</h3>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Cell</th><th>Lab trades</th><th>Lab gross</th>"
        "<th>Lab after friction</th><th>New file trades</th>"
        "<th>New file gross</th><th>New file after friction</th>"
        "<th>Verdict</th>"
        f"</tr></thead><tbody>{''.join(slice_rows)}</tbody></table></div>"
        "<p class='muted'>Both cells reproduce the lab to the cent on the "
        "new file — the deterministic futures tape behaves exactly as the "
        "SPY tape did not. The route is verified; the 24-month numbers "
        "below are trustworthy as backtests of this tape.</p>"
        "<h3>MES — full 24 months</h3>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Cell</th><th>Days</th><th>Trades</th><th>W/L</th>"
        "<th>Win rate</th><th>Gross $</th><th>Net R</th>"
        "<th>After friction</th><th>End equity (AF)</th><th>Max DD</th>"
        "<th>H1 AF</th><th>H2 AF</th><th>Trough (AF)</th><th>Skipped</th>"
        "<th>Criteria</th><th>Verdict</th>"
        f"</tr></thead><tbody>{''.join(run_rows)}</tbody></table></div>"
        "<p class='muted'>Criteria, frozen before the runs: (a) 24-month "
        "net-after-friction &gt; $0, (b) second-half net-after-friction "
        "&gt; $0, (c) after-friction realized-equity trough &gt; $0. Both "
        "strategies are positive GROSS on MES over 24 months — and both "
        "are negative after $4.90/contract friction at micro size. The "
        "gross edges are real but thinner than the cost of trading them "
        "at 1–5 contracts.</p>"
        f"{pf_panel}"
        "<div class='panel'><h2 style='margin-top:0'>How to read a MES "
        "result</h2>"
        f"<p>{html.escape(doc['framing_note'])}</p>"
        f"<p class='muted'>{html.escape(doc['data_quality_note'])}</p>"
        "</div>"
    )


def polygon_section() -> str:
    """SPY final verdict — Polygon (SIP) tape (run_polygon_spy.py).

    The four SPY cells re-run on Polygon.io's consolidated tape
    after the Databento DBEQ.BASIC feed proved to be a sampled,
    non-reproducible fraction of the market. Reads
    results/polygon_verdict.json; returns "" when the re-check has
    not been run so the rest of the dashboard still builds.
    """
    path = ROOT / "results" / "polygon_verdict.json"
    if not path.exists():
        return ""
    doc = json.loads(path.read_text())
    tape = doc["tape_verification"]
    cells = doc["cells"]
    pf = doc["portfolio"]
    pfc = pf["polygon"]["combined"]

    def af_cls(v) -> str:
        return "pos" if v > 0 else "neg"

    def crit_marks(c) -> str:
        p = c["pass"]
        marks = []
        for key, label in (
                ("a_net_after_friction_positive", "a"),
                ("b_h2_net_after_friction_positive", "b"),
                ("c_trough_above_zero", "c")):
            marks.append(f"{label} {'✓' if p[key] else '✗'}")
        return " · ".join(marks)

    vol_rows = []
    for r in tape["consolidation"]["sample_days"]:
        p, d = r["polygon"], r["databento"]
        vol_rows.append(
            f"<tr><td>{html.escape(r['date'])}</td>"
            f"<td>{p['day_volume']:,.0f}</td>"
            f"<td>{p['rth_volume']:,.0f}</td>"
            f"<td>{d['day_volume']:,.0f}</td>"
            f"<td><b>{r['polygon_to_databento_day_volume']}×</b></td>"
            f"</tr>"
        )
    det = tape["determinism"]
    cs = tape["cross_sample"]
    tape_panel = (
        "<div class='panel'><h2 style='margin-top:0'>First, is the "
        "Polygon tape real?</h2>"
        "<p>Three checks, run before any strategy number was "
        "trusted:</p>"
        "<p><b>(1) Consolidated volume.</b> SPY really trades "
        "40–90M shares a day. The Databento DBEQ.BASIC files sum to "
        "~2M a day — a sampled fraction of the market, which is why "
        "two downloads of 'the same' history could disagree. "
        "Polygon's sums land in the real range:</p>"
        "<div class='tblwrap'><table><thead><tr><th>Day</th>"
        "<th>Polygon day volume</th><th>Polygon RTH volume</th>"
        "<th>Databento day volume</th><th>Ratio</th></tr></thead>"
        f"<tbody>{''.join(vol_rows)}</tbody></table></div>"
        f"<p><b>(2) Determinism.</b> An independent re-pull of "
        f"2026-06-15 → 2026-06-19 returned "
        f"{det['repull_bars']:,} bars, timestamp-for-timestamp "
        f"identical to the main pull, with "
        f"<b>{det['bars_differing_ohlcv']} bars differing</b> in "
        f"OHLCV. Same request, same tape — the property DBEQ.BASIC "
        f"lacked.</p>"
        f"<p><b>(3) How different are the tapes?</b> On "
        f"{cs['shared_rth_timestamps']:,} shared RTH timestamps, "
        f"{cs['bars_differing_close_gt_0.001']:,} "
        f"({cs['share_differing'] * 100:.1f}%) differ in close — "
        f"but the median difference is only "
        f"${cs['median_abs_dclose_differing']:.3f} (the two "
        f"Databento pulls differed by a median $0.11). A sampled "
        f"feed's 'close' is essentially a random print inside the "
        f"minute; the SIP close is the actual last trade. "
        f"Cent-level differences, thousand-dollar consequences "
        f"below.</p></div>"
    )

    run_rows = []
    for vc in cells:
        c = vc["polygon"]
        h = c["halves"]
        v = c["verdict"]
        vcls = "pos" if v == "PASS" else "neg"
        run_rows.append(
            f"<tr><td>{html.escape(c['id'].replace('_spy_poly', ''))}</td>"
            f"<td>{c['trading_days']}</td><td>{c['trades']}</td>"
            f"<td>{c['wins']}W / {c['losses']}L</td>"
            f"<td>{pct(c['win_rate'])}</td>"
            f"<td class='{af_cls(c['net_usd'])}'>{signed_money(c['net_usd'])}</td>"
            f"<td>{c['net_r']:+.2f}R</td>"
            f"<td class='{af_cls(c['net_usd_after_friction'])}'><b>{signed_money(c['net_usd_after_friction'])}</b></td>"
            f"<td>{money(c['ending_equity_usd_after_friction'])}</td>"
            f"<td>{money(c['max_drawdown_usd'])}</td>"
            f"<td class='{af_cls(h['h1']['net_after_friction_usd'])}'>{signed_money(h['h1']['net_after_friction_usd'])}</td>"
            f"<td class='{af_cls(h['h2']['net_after_friction_usd'])}'>{signed_money(h['h2']['net_after_friction_usd'])}</td>"
            f"<td class='{af_cls(c['trough_after_friction_usd'])}'>{money(c['trough_after_friction_usd'])}"
            f"<span class='sub'> {html.escape(str(c['trough_after_friction_at']))}</span></td>"
            f"<td>{c['skipped_for_size']}</td>"
            f"<td>{crit_marks(c)}</td>"
            f"<td class='{vcls}'><b>{v}</b></td></tr>"
        )

    cmp_rows = []
    for vc in cells:
        db, pg = vc["databento_24mo"], vc["polygon"]
        conf = vc["confirmed"]
        ccls = "pos" if conf else "neg"
        cmp_rows.append(
            f"<tr><td>{html.escape(vc['strategy'])}</td>"
            f"<td class='{af_cls(db['net_usd_after_friction'])}'>{signed_money(db['net_usd_after_friction'])}</td>"
            f"<td>{html.escape(db['verdict'])}</td>"
            f"<td class='{af_cls(pg['net_usd_after_friction'])}'><b>{signed_money(pg['net_usd_after_friction'])}</b></td>"
            f"<td>{html.escape(pg['verdict'])}</td>"
            f"<td class='{ccls}'><b>{'CONFIRMED' if conf else 'NOT CONFIRMED'}</b></td></tr>"
        )

    bottom_lines = {
        "ib_break": (
            "<b>IB breakout — NOT CONFIRMED, and this is the "
            "headline.</b> The validation's one clean survivor "
            "collapses on the real tape: after-friction falls from "
            "+$5,119.23 to +$644.31, the first half goes negative "
            "(−$190.97), and the realized trough is −$1,785.72 "
            "(2026-04-10) — a real $500 account would have been "
            "wiped out in the second year. The Databento 'edge' was "
            "substantially an artifact of the sampled feed."),
        "fcr_v0": (
            "<b>FCR V0 — NOT CONFIRMED.</b> Negative after friction "
            "on both tapes (−$181.55 Databento, −$3,072.80 Polygon); "
            "on the SIP tape the second half alone loses $5,878.73. "
            "Dead on SPY under every sample."),
        "vwap_trend": (
            "<b>VWAP trend — CONFIRMED.</b> Passes all three frozen "
            "criteria on the SIP tape (+$999.10 after friction, "
            "trough +$171.81 — it failed only the trough test on "
            "Databento, where it was +$4,013.33). Real but more "
            "modest on the true tape, and thin in the second half "
            "(+$124.29): an edge, not a fortune."),
        "orb15": (
            "<b>ORB-15 — CONFIRMED, the strongest SPY cell.</b> "
            "Passes on both tapes at similar magnitudes "
            "(+$6,803.40 Polygon vs +$7,157.36 Databento after "
            "friction; both halves positive on both). Its earlier "
            "FLIPPED label came from the small 120-day lab window, "
            "where both samples straddle zero; over 24 months the "
            "two tapes agree."),
    }
    bottom = "".join(
        f"<p>{bottom_lines[vc['strategy']]}</p>"
        for vc in cells if vc["strategy"] in bottom_lines)

    pf_panel = (
        "<div class='panel'><h2 style='margin-top:0'>SPY portfolio "
        "on the SIP tape — <span class='neg'>NOT CONFIRMED</span>"
        "</h2>"
        f"<p>The same trio (IB breakout + FCR V0 + VWAP trend) in "
        f"one shared $500 account, $300 open-risk cap: "
        f"{pfc['trades_taken']} of {pfc['trades_considered']} "
        f"trades taken ({pfc['trades_skipped_by_cap']} skipped by "
        f"the cap), combined <b>{signed_money(pfc['net_usd'])}</b> "
        f"after friction, {pct(pfc['win_rate'])} win rate, trough "
        f"{money(pfc['trough_usd'])} on "
        f"{html.escape(str(pfc['trough_at']))}, realized-curve max "
        f"drawdown {money(pfc['max_drawdown_usd'])}. The Databento "
        f"portfolio's +$8,150.33 PASS does not survive the real "
        f"tape: FCR V0 alone drags −$3,072.80 standalone and IB "
        f"breakout's contribution shrinks to +$644.31. Naive "
        f"uncapped sum of the components: "
        f"{signed_money(pfc['naive_uncapped_sum_usd'])}. "
        f"Realized-P&amp;L curve only (no mark-to-market).</p>"
        "</div>"
    )

    return (
        "<h2>SPY final verdict — Polygon (SIP) tape</h2>"
        "<p class='muted'>The arbiter run. Every SPY verdict above "
        "rests on Databento's DBEQ.BASIC feed, which the validation "
        "proved is a sampled, non-reproducible fraction of the "
        "market. The four SPY cells were therefore re-run on "
        "Polygon.io's consolidated (SIP) 1-minute aggregates — "
        "the tape the trades would actually have printed on — "
        "over the same 24 months (2024-10-21 → 2026-10-19), with "
        "the identical frozen spec, the same $0.04/share friction "
        "and the same pass test: (a) 24-month net-after-friction "
        "&gt; $0, (b) second half &gt; $0, (c) after-friction "
        "realized-equity trough &gt; $0. A cell is CONFIRMED only "
        "if it passes on Polygon AND was positive after friction "
        "on the Databento 24mo file. Full record: "
        "results/polygon_verdict.json (harness: run_polygon_spy.py, "
        "data: fetch_polygon_spy.py).</p>"
        f"{tape_panel}"
        "<h3>SPY — full 24 months on the Polygon SIP tape</h3>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Cell</th><th>Days</th><th>Trades</th><th>W/L</th>"
        "<th>Win rate</th><th>Gross $</th><th>Net R</th>"
        "<th>After friction</th><th>End equity (AF)</th><th>Max DD</th>"
        "<th>H1 AF</th><th>H2 AF</th><th>Trough (AF)</th>"
        "<th>Skipped</th><th>Criteria</th><th>Verdict</th>"
        f"</tr></thead><tbody>{''.join(run_rows)}</tbody></table></div>"
        "<h3>Three-sample verdict — Databento vs Polygon</h3>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Strategy</th><th>Databento AF</th><th>Databento verdict</th>"
        "<th>Polygon AF</th><th>Polygon verdict</th><th>Final call</th>"
        f"</tr></thead><tbody>{''.join(cmp_rows)}</tbody></table></div>"
        f"{pf_panel}"
        "<div class='panel'><h2 style='margin-top:0'>Bottom line "
        "per strategy</h2>"
        f"{bottom}"
        "<p class='muted'>What survives the arbiter: ORB-15 and "
        "VWAP trend on SPY. What does not: the IB breakout's "
        "headline number, FCR on SPY, and the trio portfolio built "
        "from the Databento winners. Costs are still the lab's "
        "friction model ($0.04/share round trip); no slippage "
        "beyond it is modeled.</p></div>"
    )


def orb15_deepdive_section() -> str:
    """ORB-15 deep dive (run_orb15_deepdive.py).

    Matt's redirect after the Polygon verdict: focus entirely on
    the confirmed lead strategy. Diagnostics on the 498-trade
    Polygon log (after-friction basis) plus pre-declared robustness
    — friction at 2x/3x, a 1-bar execution lag, sub-period
    stability. The spec itself is frozen in ORB15_SPEC.md. Reads
    results/orb15_deepdive.json; returns "" when the deep dive has
    not been run.
    """
    path = ROOT / "results" / "orb15_deepdive.json"
    if not path.exists():
        return ""
    doc = json.loads(path.read_text())
    dg = doc["diagnostics"]
    rb = doc["robustness"]
    dist = dg["distribution"]
    dd = dg["drawdown"]

    def af_cls(v) -> str:
        return "pos" if v > 0 else "neg"

    def crit_marks(p) -> str:
        marks = []
        for key, label in (
                ("a_net_after_friction_positive", "a"),
                ("b_h2_net_after_friction_positive", "b"),
                ("c_trough_above_zero", "c")):
            marks.append(f"{label} {'✓' if p[key] else '✗'}")
        return " · ".join(marks)

    month_rows = "".join(
        f"<tr><td>{html.escape(m['month'])}</td><td>{m['trades']}</td>"
        f"<td>{pct(m['win_rate']) if m['win_rate'] is not None else '—'}</td>"
        f"<td class='{af_cls(m['af_usd'])}'>{signed_money(m['af_usd'])}</td></tr>"
        for m in dg["monthly"])
    quarter_rows = "".join(
        f"<tr><td>{html.escape(q['quarter'])}</td><td>{q['trades']}</td>"
        f"<td>{pct(q['win_rate']) if q['win_rate'] is not None else '—'}</td>"
        f"<td class='{af_cls(q['af_usd'])}'>{signed_money(q['af_usd'])}</td></tr>"
        for q in dg["quarterly"])
    red_months = sum(1 for m in dg["monthly"] if m["af_usd"] < 0)

    ls = dg["long_short"]
    ls_rows = "".join(
        f"<tr><td>{side}</td><td>{s['trades']}</td>"
        f"<td>{pct(s['win_rate'])}</td>"
        f"<td class='{af_cls(s['af_usd'])}'>{signed_money(s['af_usd'])}</td></tr>"
        for side, s in (("Long", ls["long"]), ("Short", ls["short"])))
    exit_rows = "".join(
        f"<tr><td>{html.escape(r)}</td><td>{s['trades']}</td>"
        f"<td>{pct(s['win_rate'])}</td>"
        f"<td class='{af_cls(s['af_usd'])}'>{signed_money(s['af_usd'])}</td></tr>"
        for r, s in dg["exit_reasons"].items())
    bucket_rows = "".join(
        f"<tr><td>{html.escape(b['bucket'])}</td><td>{b['trades']}</td>"
        f"<td>{pct(b['win_rate'])}</td>"
        f"<td class='{af_cls(b['af_usd'])}'>{signed_money(b['af_usd'])}</td></tr>"
        for b in dg["entry_time_buckets"])

    rob_rows = []
    for f in rb["friction_stress"]:
        v = "PASS" if f["pass"]["passed"] else "FAIL"
        rob_rows.append(
            f"<tr><td>Friction {f['multiple']:.0f}× — "
            f"${f['friction_per_unit_usd']:.2f}/share"
            f"{' (frozen)' if f['multiple'] == 1.0 else ''}</td>"
            f"<td>{f['trades']}</td>"
            f"<td class='{af_cls(f['net_usd_after_friction'])}'><b>{signed_money(f['net_usd_after_friction'])}</b></td>"
            f"<td class='{af_cls(f['halves']['h2']['net_after_friction_usd'])}'>{signed_money(f['halves']['h2']['net_after_friction_usd'])}</td>"
            f"<td>{money(f['trough_after_friction_usd'])}</td>"
            f"<td>{crit_marks(f['pass'])}</td>"
            f"<td class='{'pos' if f['pass']['passed'] else 'neg'}'><b>{v}</b></td></tr>")
    lag = rb["execution_lag"]
    v = "PASS" if lag["pass"]["passed"] else "FAIL"
    rob_rows.append(
        f"<tr><td>Execution lag — fill at next bar's open"
        f"<span class='sub'> ({lag['lag_voided']} voided, "
        f"{lag['skipped_for_size']} skipped)</span></td>"
        f"<td>{lag['trades']}</td>"
        f"<td class='{af_cls(lag['net_usd_after_friction'])}'><b>{signed_money(lag['net_usd_after_friction'])}</b></td>"
        f"<td class='{af_cls(lag['halves']['h2']['net_after_friction_usd'])}'>{signed_money(lag['halves']['h2']['net_after_friction_usd'])}</td>"
        f"<td>{money(lag['trough_after_friction_usd'])}</td>"
        f"<td>{crit_marks(lag['pass'])}</td>"
        f"<td class='{'pos' if lag['pass']['passed'] else 'neg'}'><b>{v}</b></td></tr>")
    subs = rb["subperiods"]
    sub_line = " · ".join(
        f"{b['bucket'].replace('_partial', ' (partial)')} "
        f"<span class='{af_cls(b['af_usd'])}'>{signed_money(b['af_usd'])}</span>"
        for b in subs["buckets"])
    uw = dd["longest_underwater"]
    b10 = dist["best_10_days"]

    return (
        "<h2>ORB-15 deep dive — the confirmed lead, under the "
        "microscope</h2>"
        "<p class='muted'>The Polygon verdict made ORB-15 the "
        "project's lead SPY strategy, so it gets the full workup: "
        "every diagnostic below is computed on its 498-trade "
        "Polygon log on an after-friction basis (per-trade AF = "
        "gross − $0.04/share; a 'win' is AF &gt; 0, so win rates "
        "here can differ slightly from the gross headline). The "
        "strategy itself is frozen in <b>ORB15_SPEC.md</b> "
        "(project root, also on GitHub) — a future forward test "
        "must match that document exactly. Full record: "
        "results/orb15_deepdive.json (harness: "
        "run_orb15_deepdive.py).</p>"
        "<div class='panel'><h2 style='margin-top:0'>Where the "
        "money comes from</h2>"
        f"<p><b>Direction.</b> Near-identical win rates, very "
        f"different money: the short side produced "
        f"{signed_money(ls['short']['af_usd'])} of the "
        f"{signed_money(ls['long']['af_usd'] + ls['short']['af_usd'])} "
        f"total. On this tape, ORB-15 is mostly a short-side "
        f"strategy.</p>"
        "<div class='tblwrap'><table><thead><tr><th>Direction</th>"
        "<th>Trades</th><th>Win rate (AF)</th><th>AF $</th></tr>"
        f"</thead><tbody>{ls_rows}</tbody></table></div>"
        "<p style='margin-top:14px'><b>Exits.</b> The 2R target "
        "does the heavy lifting; the 15:55 flatten cohort — trades "
        "that ran in the money without reaching 2R — is solidly "
        "profitable too (73% win rate).</p>"
        "<div class='tblwrap'><table><thead><tr><th>Exit reason</th>"
        "<th>Trades</th><th>Win rate (AF)</th><th>AF $</th></tr>"
        f"</thead><tbody>{exit_rows}</tbody></table></div>"
        "<p style='margin-top:14px'><b>Entry time.</b> Most "
        "triggers fire in the first bucket (09:45–09:59 closes), "
        "but the 10:00–10:29 bucket converts better and earned "
        "more from a quarter of the trades. Entries after 11:00 "
        "are rare and worthless here.</p>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Entry bucket (ET)</th><th>Trades</th>"
        "<th>Win rate (AF)</th><th>AF $</th></tr>"
        f"</thead><tbody>{bucket_rows}</tbody></table></div></div>"
        "<div class='panel'><h2 style='margin-top:0'>Monthly "
        "consistency</h2>"
        f"<p>{red_months} of {len(dg['monthly'])} months were "
        f"red. The payoff shape is mechanical: wins cap near "
        f"+$297 (2R of ~$150 risk, less friction) and losses near "
        f"−$162, so no single day makes or breaks the strategy — "
        f"but the 10 best days still account for "
        f"{b10['share_of_total_net_pct']:.0f}% of the total net "
        f"profit ({signed_money(b10['af_usd'])}).</p>"
        "<div class='tblwrap'><table><thead><tr><th>Month</th>"
        "<th>Trades</th><th>Win rate (AF)</th><th>AF $</th></tr>"
        f"</thead><tbody>{month_rows}</tbody></table></div>"
        "<div class='tblwrap' style='margin-top:10px'><table>"
        "<thead><tr><th>Quarter</th><th>Trades</th>"
        "<th>Win rate (AF)</th><th>AF $</th></tr></thead>"
        f"<tbody>{quarter_rows}</tbody></table></div>"
        f"<p style='margin-top:10px'>Average win "
        f"{signed_money(dist['avg_win_usd'])}, average loss "
        f"{signed_money(dist['avg_loss_usd'])}; longest streaks "
        f"{dist['longest_win_streak']} wins / "
        f"{dist['longest_loss_streak']} losses. Best day "
        f"{html.escape(dist['best_days'][0]['date'])} "
        f"({signed_money(dist['best_days'][0]['af_usd'])}); worst "
        f"day {html.escape(dist['worst_days'][0]['date'])} "
        f"({signed_money(dist['worst_days'][0]['af_usd'])}).</p>"
        "</div>"
        "<div class='panel'><h2 style='margin-top:0'>Drawdown "
        "character — the honest cost</h2>"
        f"<p>Maximum drawdown on the after-friction realized "
        f"curve: <b>{money(dd['max_drawdown_usd'])}</b>, from the "
        f"{html.escape(str(dd['peak_date']))} peak "
        f"({money(dd['peak_equity_usd'])}) to "
        f"{html.escape(str(dd['trough_date']))} "
        f"({money(dd['trough_equity_usd'])}). The account then "
        f"spent <b>{uw['trading_days']} consecutive trading "
        f"days</b> below that peak and had <b>not recovered by "
        f"the end of the window</b> — the strategy's worst stretch "
        f"is its most recent one. The frozen trough test (never "
        f"below $0) is about the early equity path, not this: a "
        f"forward test must be sized to sit through a ~$2,000, "
        f"months-long drawdown without flinching.</p></div>"
        "<div class='panel'><h2 style='margin-top:0'>Robustness — "
        "pre-declared stress tests, spec unchanged</h2>"
        "<div class='tblwrap'><table><thead><tr><th>Stress</th>"
        "<th>Trades</th><th>AF $</th><th>H2 AF</th><th>Trough</th>"
        "<th>Criteria</th><th>Verdict</th></tr></thead>"
        f"<tbody>{''.join(rob_rows)}</tbody></table></div>"
        f"<p style='margin-top:10px'>The edge survives double the "
        f"modeled friction and a full minute of execution lag. At "
        f"triple friction it fails — on the second-half criterion "
        f"only; the 24-month total stays positive.</p>"
        f"<p><b>Sub-periods:</b> {sub_line} — "
        f"{subs['green_count']} of {subs['bucket_count']} buckets "
        f"green, but decaying: each of the last three buckets is "
        f"smaller than the one before, and the final partial "
        f"bucket is barely above water. Confirmed is not the same "
        f"as strengthening.</p></div>"
    )


# ============================================================ ORB15-MES-BEGIN
def orb15_mes_section() -> str:
    """ORB-15 on futures (MES) — 24-month validation
    (run_orb15_mes_validation.py).

    The frozen ORB-15 strategy re-run on the deterministic ES
    futures tape, traded as MES — the gate for taking ORB-15 live
    at a $500 account, where MES is the only faithful executable
    form. Reads results/orb15_mes_validation.json; returns ""
    when the validation has not been run so the rest of the
    dashboard still builds.
    """
    path = ROOT / "results" / "orb15_mes_validation.json"
    if not path.exists():
        return ""
    doc = json.loads(path.read_text())
    if doc.get("status") != "COMPLETE":
        return ""  # aborted/partial record — nothing honest to show
    c = doc["cell"]
    g = doc["diagnostics"]
    cmp_ = doc["spy_comparison"]
    spy = cmp_["spy_reference"]
    mes = cmp_["mes"]
    h = c["halves"]
    s = doc["slice_verification"]
    e, got = s["expected"], s["got"]

    def af_cls(v) -> str:
        return "pos" if v > 0 else "neg"

    def crit_marks(cell) -> str:
        p = cell["pass"]
        marks = []
        for key, label in (
                ("a_net_after_friction_positive", "a"),
                ("b_h2_net_after_friction_positive", "b"),
                ("c_trough_above_zero", "c")):
            marks.append(f"{label} {'✓' if p[key] else '✗'}")
        return " · ".join(marks)

    v = c["verdict"]
    vcls = "pos" if v == "PASS" else "neg"
    ls = g["long_short"]
    rs = g["risk_shape"]
    subs = g["subperiods"]
    sub_line = " · ".join(
        f"{b['bucket']} {signed_money(b['af_usd'])}"
        for b in subs["buckets"])
    mon = g["monthly"]
    sp = rs["stop_pts"]
    hist_line = ", ".join(
        f"{k} contracts × {n}" for k, n in
        rs["contracts_histogram"].items())
    friction_total = c["total_friction_usd"]
    friction_pct = (round(100 * friction_total / c["net_usd"])
                    if c["net_usd"] else 0)

    return (
        "<h2>ORB-15 on futures (MES) — 24-month validation</h2>"
        "<p class='muted'>The gate for the live-$500 question. ORB-15 "
        "as confirmed on SPY needs ~100 shares a trade (~$77k "
        "notional) to risk its $150 — no $500 stock account can run "
        "it. MES futures can: the same frozen strategy "
        "(ORBStrategy, 15-minute opening range, per ORB15_SPEC.md) "
        "on the deterministic ES.v.0 tape (GLBX.MDP3), traded as "
        "MES at $5/pt over the same window as the SPY Polygon "
        "validation (2024-10-21 → 2026-10-19). The open question is "
        "the tape: ES trades nearly around the clock, so its 9:30 "
        "ET 'open' is an overnight continuation, not the cash "
        "auction open — the effect that halved IB breakout's edge "
        "in the MES cross-check above. Full record: "
        "results/orb15_mes_validation.json (harness: "
        "run_orb15_mes_validation.py).</p>"
        "<p><b>Sizing rule (futures convention, stated in full):</b> "
        f"{html.escape(doc['spec']['sizing_rule'])}</p>"
        "<h3>Slice verification — lab window (2026-06-22 → "
        "2026-10-20) on the 24-month ES file</h3>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Cell</th><th>Lab trades</th><th>Lab gross</th>"
        "<th>Lab after friction</th><th>New file trades</th>"
        "<th>New file gross</th><th>New file after friction</th>"
        "<th>Verdict</th>"
        "</tr></thead><tbody>"
        f"<tr><td>orb15_mes</td><td>{e['trades']}</td>"
        f"<td>{signed_money(e['net_usd'])}</td>"
        f"<td>{signed_money(e['net_after_friction'])}</td>"
        f"<td>{got['trades']}</td>"
        f"<td>{signed_money(got['net_usd'])}</td>"
        f"<td>{signed_money(got['net_after_friction'])}</td>"
        f"<td class='pos'><b>{'MATCH' if s['match'] else 'MISMATCH'}</b></td></tr>"
        "</tbody></table></div>"
        "<p class='muted'>The cell reproduces the lab to the cent "
        "on the 24-month file — the route is verified before any "
        "24-month number is trusted.</p>"
        "<h3>MES — full 24 months</h3>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Cell</th><th>Days</th><th>Trades</th><th>W/L</th>"
        "<th>Win rate</th><th>Gross $</th><th>Net R</th>"
        "<th>After friction</th><th>End equity (AF)</th>"
        "<th>Max DD (gross)</th><th>H1 AF</th><th>H2 AF</th>"
        "<th>Trough (AF)</th><th>Skipped</th><th>Criteria</th>"
        "<th>Verdict</th>"
        "</tr></thead><tbody>"
        f"<tr><td>orb15_mes</td>"
        f"<td>{c['trading_days']}</td><td>{c['trades']}</td>"
        f"<td>{c['wins']}W / {c['losses']}L</td>"
        f"<td>{pct(c['win_rate'])}</td>"
        f"<td class='{af_cls(c['net_usd'])}'>{signed_money(c['net_usd'])}</td>"
        f"<td>{c['net_r']:+.2f}R</td>"
        f"<td class='{af_cls(c['net_usd_after_friction'])}'><b>{signed_money(c['net_usd_after_friction'])}</b></td>"
        f"<td>{money(c['ending_equity_usd_after_friction'])}</td>"
        f"<td>{money(c['max_drawdown_usd'])}</td>"
        f"<td class='{af_cls(h['h1']['net_after_friction_usd'])}'>{signed_money(h['h1']['net_after_friction_usd'])}</td>"
        f"<td class='{af_cls(h['h2']['net_after_friction_usd'])}'>{signed_money(h['h2']['net_after_friction_usd'])}</td>"
        f"<td class='{af_cls(c['trough_after_friction_usd'])}'>{money(c['trough_after_friction_usd'])}"
        f"<span class='sub'> {html.escape(str(c['trough_after_friction_at']))}</span></td>"
        f"<td>{c['skipped_for_size']}</td>"
        f"<td>{crit_marks(c)}</td>"
        f"<td class='{vcls}'><b>{v}</b></td></tr>"
        f"<tr><td colspan='16' class='sub'>Friction paid over the "
        f"run: {money(friction_total)} ($4.90/contract round trip "
        f"at an average of {c['avg_units']} contracts/trade) — "
        f"{friction_pct}% of the gross. After-friction max "
        f"drawdown on the realized curve: "
        f"{money(g['af_max_drawdown_usd'])}."
        f"</td></tr>"
        "</tbody></table></div>"
        "<p class='muted'>Criteria, frozen and identical to the SPY "
        "verdict: (a) 24-month net-after-friction &gt; $0, (b) "
        "second-half net-after-friction &gt; $0, (c) after-friction "
        "realized-equity trough &gt; $0.</p>"
        "<h3>Side by side — MES vs SPY (Polygon SIP)</h3>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>Tape</th><th>Days</th><th>Trades</th><th>Win rate</th>"
        "<th>Gross $</th><th>Gross / trade</th><th>After friction</th>"
        "<th>AF / trade</th><th>Trough (AF)</th><th>Skipped</th>"
        "</tr></thead><tbody>"
        f"<tr><td>SPY (Polygon)</td><td>{spy['trading_days']}</td>"
        f"<td>{spy['trades']}</td><td>{pct(spy['win_rate'])}</td>"
        f"<td>{signed_money(spy['net_usd'])}</td>"
        f"<td>{signed_money(spy['gross_per_trade_usd'])}</td>"
        f"<td><b>{signed_money(spy['net_usd_after_friction'])}</b></td>"
        f"<td>{signed_money(spy['af_per_trade_usd'])}</td>"
        f"<td>{money(spy['trough_after_friction_usd'])}</td>"
        f"<td>{spy['skipped_for_size']}</td></tr>"
        f"<tr><td>MES (GLBX)</td><td>{mes['trading_days']}</td>"
        f"<td>{mes['trades']}</td><td>{pct(mes['win_rate'])}</td>"
        f"<td>{signed_money(mes['net_usd'])}</td>"
        f"<td>{signed_money(mes['gross_per_trade_usd'])}</td>"
        f"<td><b>{signed_money(mes['net_usd_after_friction'])}</b></td>"
        f"<td>{signed_money(mes['af_per_trade_usd'])}</td>"
        f"<td>{money(mes['trough_after_friction_usd'])}</td>"
        f"<td>{mes['skipped_for_size']}</td></tr>"
        "</tbody></table></div>"
        f"<p>The per-trade after-friction edge on MES is "
        f"<b>{signed_money(mes['af_per_trade_usd'])}</b> vs SPY's "
        f"<b>{signed_money(spy['af_per_trade_usd'])}</b> — a ratio of "
        f"{cmp_['af_per_trade_ratio_mes_over_spy']:.2f}×. The "
        "overnight-continuation effect is real and it is almost "
        "exactly the halving the cross-check predicted; the gross "
        "edge only falls ~15%, but $4.90 × ~1.6 contracts of "
        "friction per trade (vs ~$3.62/trade in SPY shares) takes "
        "the rest. Unlike IB breakout, ORB-15's edge is large "
        "enough that half of it still clears the bar.</p>"
        "<div class='panel'><h2 style='margin-top:0'>Decay, shape, "
        "and the honest caveats</h2>"
        f"<p><b>Sub-periods (AF):</b> {sub_line} — "
        f"{subs['green_count']} of {subs['bucket_count']} buckets "
        "green, and the pattern rhymes with SPY's decay rather "
        "than refuting it: H1 made "
        f"{signed_money(h['h1']['net_after_friction_usd'])} of the "
        f"{signed_money(c['net_usd_after_friction'])} total, H2 "
        f"only {signed_money(h['h2']['net_after_friction_usd'])}, "
        "and the final partial bucket (Jul–Oct 2026) is the run's "
        "only red one. The gate is passed; the trend through the "
        "gate is still downward.</p>"
        f"<p><b>Long/short:</b> shorts {ls['short']['trades']} "
        f"trades, {signed_money(ls['short']['af_usd'])} AF "
        f"({ls['short']['af_share_of_total'] * 100:.0f}% of the "
        f"total); longs {ls['long']['trades']} trades, "
        f"{signed_money(ls['long']['af_usd'])} AF. SPY's "
        "short-side dominance (~80%) broadly holds on MES.</p>"
        f"<p><b>Months:</b> {mon['green']} green / {mon['red']} "
        f"red of {mon['months_with_trades']} months with trades.</p>"
        f"<p><b>Risk shape:</b> stop distances run "
        f"{sp['min']}–{sp['max']} ES points (median {sp['median']}, "
        f"mean {sp['mean']}). Contract counts: {hist_line} — only "
        f"{rs['trades_at_5_contract_cap']} trades sit at the "
        "5-contract cap, and "
        f"{c['skipped_for_size']} signal days were skipped for "
        "size entirely (stop wider than 30 points risks more "
        "than the $150 budget even at 1 contract). A live $500 "
        "account mostly trades 1–2 contracts of this.</p>"
        f"<p>{html.escape(doc['framing_note'])}</p>"
        f"<p class='muted'>{html.escape(doc['data_quality_note'])}</p>"
        "</div>"
    )
# ============================================================ ORB15-MES-END


def strategy_lab_section() -> str:
    """Strategy Lab (build_strategy_lab.py + build_portfolio.py).

    Eight professional tactics — the intraday pack, the daily pair,
    and FCR V0 as the reference column — over one 120-day window,
    ranked by net-after-friction $. Reads results/strategy_lab.json
    and results/portfolio.json; returns "" when the lab has not been
    run so the rest of the dashboard still builds.
    """
    path = ROOT / "results" / "strategy_lab.json"
    if not path.exists():
        return ""
    lab = json.loads(path.read_text())
    runs = lab["runs"]
    by_id = {r["id"]: r for r in runs}
    ordered = [by_id[i] for i in lab["rankings"]["by_net_after_friction"]]
    green = [r for r in runs if r["net_after_friction_usd"] > 0]
    anecdotes = [r for r in green if r["anecdote_class"]]
    spy_top = sum(1 for r in ordered[:10] if r["symbol"] == "SPY")

    rows_html = []
    for r in ordered:
        after = r["net_after_friction_usd"]
        cls = "pos" if after > 0 else ("neg" if after < 0 else "")
        hl = " style='background:#12241a'" if after > 0 else ""
        mark = " †" if r["anecdote_class"] else ""
        rows_html.append(
            f"<tr{hl}>"
            f"<td>{r['rank_net_after_friction']}</td>"
            f"<td>{html.escape(r['strategy_label'])} — {html.escape(r['symbol'])}{mark}</td>"
            f"<td>{r['trading_days']}</td><td>{r['trades']}</td>"
            f"<td>{r['wins']}W / {r['losses']}L</td>"
            f"<td>{pct(r['win_rate'])}</td>"
            f"<td>{signed_money(r['net_usd'])}</td>"
            f"<td class='{cls}'><b>{signed_money(after)}</b></td>"
            f"<td>{r['net_r']:+.2f}R</td>"
            f"<td>{money(r['ending_equity_usd'])}</td>"
            f"<td>{money(r['max_drawdown_usd'])}</td>"
            f"<td>{r['skipped_for_size']}</td></tr>"
        )

    pf_path = ROOT / "results" / "portfolio.json"
    pf_panel = ""
    if pf_path.exists():
        pf = json.loads(pf_path.read_text())
        cb = pf["combined"]
        comp_rows = "".join(
            f"<tr><td>{html.escape(by_id[c['run_id']]['strategy_label'])} — "
            f"{html.escape(c['symbol'])}</td>"
            f"<td>{signed_money(c['standalone']['net_after_friction_usd'])}</td>"
            f"<td>{c['trades_taken']} / {c['trades_considered']}</td>"
            f"<td>{c['trades_skipped_by_cap']}</td>"
            f"<td class='{'pos' if c['taken_net_usd'] >= 0 else 'neg'}'>"
            f"{signed_money(c['taken_net_usd'])}</td></tr>"
            for c in pf["components"]
        )
        pf_panel = (
            "<div class='panel'><h2 style='margin-top:0'>Conjunction portfolio — "
            "top 3 cells, one $500 account, $300 open-risk cap</h2>"
            "<div class='tblwrap'><table><thead><tr><th>Component</th>"
            "<th>Standalone (after friction)</th><th>Taken / considered</th>"
            "<th>Skipped by cap</th><th>Taken net</th></tr></thead>"
            f"<tbody>{comp_rows}</tbody></table></div>"
            f"<p>Combined: <b class='pos'>{signed_money(cb['net_usd'])}</b> after "
            f"friction over {cb['trades_taken']} taken trades "
            f"({cb['wins']}W / {cb['losses']}L, {pct(cb['win_rate'])} win rate), "
            f"ending equity {money(cb['ending_equity_usd'])}, realized-curve max "
            f"drawdown {money(cb['max_drawdown_usd'])}; the naive uncapped sum of "
            f"the three standalone runs is {signed_money(cb['naive_uncapped_sum_usd'])}. "
            "Selection rule: top 3 by net-after-friction $ among runs with ≥15 "
            "trades, at most one cell per strategy family. Mechanics: each trade "
            "keeps its own strategy's size and net-of-friction P&amp;L; trades are "
            "processed chronologically and skipped (counted) when their initial "
            "risk would push summed open risk past $300. <b>Concentration "
            "warning:</b> all three winning components trade SPY intraday — the "
            "diversification here is by strategy logic, not by market, and the "
            "three books are strongly correlated. Simplification: the equity "
            "curve uses realized P&amp;L at exit times only (no mark-to-market "
            "of open trades), so the drawdown shown is the realized-curve "
            "drawdown. Full record: results/portfolio.json + "
            "results/trades_portfolio_combined.csv.</p></div>"
        )

    top = ordered[0]
    framing = (
        f"<p><b>Honest framing — read this before the table.</b> The intraday "
        f"window is 120 CALENDAR days (2026-06-22 → 2026-10-20), which is only "
        f"87 regular sessions for the futures and 84 for SPY (its data ends "
        f"2026-10-19) — roughly 84 trading days, not 120. The two daily "
        f"strategies trade a 120-TRADING-day window instead, and even so "
        f"produced only a handful of trades (Donchian 5 / 5 / 0 across "
        f"SPY/QQQ/GC, RSI(2) 1 / 4) — those rows are anecdotes, not verdicts, "
        f"and every row under 15 trades is marked †.</p>"
        f"<p><b>Chance manufactures green cells.</b> {len(green)} of 45 cells "
        f"finished positive after friction — but with 45 cells on test, several "
        f"green cells are EXPECTED by luck alone, and the table is ranked by "
        f"the winners, which inflates them further (winner's curse). "
        f"{len(anecdotes)} of the green cells rest on fewer than 15 trades. "
        f"The standout cells — {html.escape(top['strategy_label'])} on "
        f"{html.escape(top['symbol'])} at {signed_money(top['net_after_friction_usd'])} "
        f"after friction over {top['trades']} trades, and the SPY-heavy top of "
        f"the table ({spy_top} of the top 10 are SPY) — have earned a "
        f"longer-window test, not belief. One window (Jun–Oct 2026) favored "
        f"SPY intraday systems; another window may not.</p>"
        f"<p><b>Friction model.</b> Gross numbers model no costs at all. The "
        f"after-friction column deducts a fixed per-unit round-trip charge — "
        f"MNQ $3.40, MES $4.90, MGC $4.40, SIL $12.40, SPY/QQQ $0.04 per "
        f"share (fees plus roughly a tick of slippage per side) — from every "
        f"trade. It is an estimate, not a fill report, and it matters: "
        f"several gross-green cells (e.g. previous-day sweep and VWAP "
        f"reversion on MNQ) flip red after friction, which is why the "
        f"ranking uses the after-friction column. As everywhere on this "
        f"dashboard, the simulator never halts for balance or margin — "
        f"several runs' paper equity goes deeply negative, which a real "
        f"$500 account could never do; read those as strategy behavior "
        f"under the house spec, not survivable account histories.</p>"
    )
    return (
        "<h2>Strategy Lab — eight pro tactics, 120 days</h2>"
        "<p class='muted'>Every tactic from the research brief "
        "(research/strategy_evidence.md — canonical rules audited against "
        "Crabel, Raschke, Connors and the Turtle literature), frozen into "
        "mechanical rules and run under the same house spec: $500 paper "
        "account, $150 risk budget per trade, futures ≤5 contracts, SPY/QQQ "
        "in shares, no costs in the gross columns. Intraday pack on "
        "1-minute Databento bars, 2026-06-22 → 2026-10-20 (SPY → 10-19): "
        "ORB 15m / ORB 30m / ORB 15m close-exit, previous-day sweep, VWAP "
        "±2σ reversion, VWAP trend pullback, Initial Balance breakout, and "
        "FCR V0 as the reference column (route proven: it reproduces the "
        "known MNQ 60-day baseline of −$374.50 / 13 trades exactly). Daily "
        "pair on Yahoo daily bars, last 120 trading days: Donchian trend "
        "and Connors RSI(2) — with the canonical Connors entry AT the "
        "signal day's close. Ranked by net-after-friction $. "
        "Full record: results/strategy_lab.json.</p>"
        "<div class='tblwrap'><table><thead><tr>"
        "<th>#</th><th>Strategy — symbol</th><th>Days</th><th>Trades</th>"
        "<th>W/L</th><th>Win rate</th><th>Net $ (gross)</th>"
        "<th>Net $ after friction</th><th>Net R</th><th>End equity</th>"
        "<th>Max DD</th><th>Skipped</th>"
        f"</tr></thead><tbody>{''.join(rows_html)}</tbody></table></div>"
        f"{pf_panel}"
        f"<div class='panel'>{framing}</div>"
    )


def main() -> int:
    summary = json.loads((ROOT / "results" / "summary.json").read_text())
    runs = {r["id"]: r for r in summary["runs"]}
    cfg = summary["config"]
    headline = runs["mnq_5m_60d"]
    onemin = runs["mnq_1m_7d"]
    qqq = runs["qqq_5m_60d"]

    chart = equity_svg(
        [
            ("MNQ 5m", headline["equity_curve"], "#4cc38a"),
            ("QQQ 5m", qqq["equity_curve"], "#6e9fff"),
        ]
    )

    all_runs_rows = "".join(
        "<tr>"
        f"<td>{r['id']}</td><td>{r['data_symbol']}</td><td>{r['bar']}</td>"
        f"<td>{r['date_range']['start']} → {r['date_range']['end']}</td>"
        f"<td>{r['trading_days']}</td><td>{r['trades']}</td>"
        f"<td>{pct(r['win_rate'])}</td>"
        f"<td class='{'pos' if r['net_usd'] >= 0 else 'neg'}'>{signed_money(r['net_usd'])}</td>"
        f"<td>{r['net_r']:+.2f}R</td><td>{money(r['ending_equity_usd'])}</td>"
        f"<td>{money(r['max_drawdown_usd'])}</td><td>{r['skipped_for_size']}</td>"
        "</tr>"
        for r in summary["runs"]
    )

    data_notes = "".join(f"<li>{html.escape(n)}</li>" for n in summary["data_notes"])
    checklist = "".join(f"<li>{html.escape(x)}</li>" for x in MATT_CHECKLIST)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FCR Paper Bot — MNQ</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0b0e13; color:#e6edf3;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:960px; margin:0 auto; padding:20px 14px 48px; }}
  h1 {{ font-size:26px; margin:4px 0 2px; }}
  h2 {{ font-size:19px; margin:28px 0 4px; }}
  .muted {{ color:#8b98a9; font-size:13px; margin:2px 0 10px; }}
  .badge {{ display:inline-block; background:#3d2e04; color:#f5c451; border:1px solid #6b5410;
            padding:2px 8px; border-radius:999px; font-size:12px; font-weight:600; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; }}
  .card {{ background:#151a22; border:1px solid #232b38; border-radius:10px; padding:12px; }}
  .card .k {{ color:#8b98a9; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  .card .v {{ font-size:20px; font-weight:650; margin-top:4px; }}
  .sub {{ color:#8b98a9; font-size:12px; font-weight:400; }}
  .pos {{ color:#4cc38a; }} .neg {{ color:#ff7b72; }}
  .panel {{ background:#151a22; border:1px solid #232b38; border-radius:10px; padding:14px; margin-top:12px; }}
  .chart {{ width:100%; height:auto; }}
  .ax {{ fill:#8b98a9; font-size:11px; }}
  .tblwrap {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th, td {{ padding:7px 8px; border-bottom:1px solid #232b38; text-align:right; white-space:nowrap; }}
  th {{ color:#8b98a9; font-weight:600; }}
  td:first-child, th:first-child {{ text-align:left; }}
  ul {{ margin:6px 0 6px 18px; padding:0; }}
  li {{ margin:5px 0; font-size:14px; line-height:1.45; }}
  .cfg {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
  .cfg span {{ background:#151a22; border:1px solid #232b38; border-radius:999px;
               padding:5px 11px; font-size:13px; }}
  footer {{ color:#5b6b82; font-size:12px; margin-top:30px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>FCR Paper Bot — MNQ</h1>
  <p class="muted">First Candle Rule · micro E-mini Nasdaq-100 futures · <span class="badge">PAPER ONLY — no live trading</span></p>
  <div class="cfg">
    <span>Account: {money(cfg['starting_balance_usd'])}</span>
    <span>MNQ: ${cfg['point_value_usd']:.0f}/pt per contract</span>
    <span>Risk budget: {money(cfg['risk_budget_usd'])}/trade</span>
    <span>Max {cfg['max_contracts']} contracts</span>
    <span>Target: {cfg['rr_target']:.0f}:1 reward:risk</span>
    <span>One trade per day</span>
    <span>First candle {cfg['first_candle']}</span>
    <span>No entries after {cfg['entry_cutoff']}</span>
    <span>Flat by {cfg['flatten_time']}</span>
  </div>

  {stat_cards(headline, "60-day functional backtest — MNQ (5-minute bars)")}

  <div class="panel">
    <h2 style="margin-top:0">Equity curve — 60-day runs (start $500)</h2>
    {chart}
    <p class="muted">QQQ (blue) shown for comparison — same rules, same $150 risk budget, sized in shares. The simulator never halts for balance or margin; see caveats.</p>
  </div>

  {stat_cards(onemin, "True 1-minute configuration — MNQ (last ~7 days)")}

  <h2>Trades — 60-day MNQ run</h2>
  {trade_table(headline)}

  <h2>All runs</h2>
  <div class="tblwrap"><table><thead><tr>
    <th>Run</th><th>Data</th><th>Bars</th><th>Range</th><th>Days</th><th>Trades</th>
    <th>Win rate</th><th>Net $</th><th>Net R</th><th>End equity</th><th>Max DD</th><th>Skipped</th>
  </tr></thead><tbody>{all_runs_rows}</tbody></table></div>

  {experiments_section()}

  {gold24_section()}

  {strategy_lab_section()}

  {validation24_section()}

  {crosscheck_mes_section()}

  {polygon_section()}

  {orb15_deepdive_section()}

  {orb15_mes_section()}

  <div class="panel">
    <h2 style="margin-top:0">Broker: Tradovate (demo first)</h2>
    <p>{html.escape(BROKER_STATUS)}</p>
    <p class="muted" style="margin-bottom:4px">What Matt needs to do:</p>
    <ul>{checklist}</ul>
  </div>

  <div class="panel">
    <h2 style="margin-top:0">Caveats — read before trusting any number here</h2>
    <ul>
      <li><b>5-minute bars for the 60-day run.</b> Free Yahoo data only goes back ~7 days at 1-minute resolution, so the 60-day test runs the pattern on 5-minute bars (first 5-min bar = the first candle; FVG/retest/engulfing all on 5-min bars). The true strategy runs on 1-minute bars — the 7-day 1-minute run above is the real configuration on a tiny sample.</li>
      <li><b>No slippage, spread, or commissions are modeled.</b> Entries fill exactly at the engulfing bar's close; exits exactly at stop/target. Real MNQ fills would be worse by a few ticks per trade, and Tradovate micro commissions (~$0.39/side + exchange/clearing/NFA fees on the free plan) are not deducted.</li>
      <li><b>Continuous-contract data.</b> Yahoo's MNQ=F is an unadjusted front-month splice — quarterly rolls appear as price jumps. The strategy only uses intraday levels, but the data is still not exchange-grade.</li>
      <li><b>The simulator never stops.</b> Equity tracking is bookkeeping only: no margin checks, no halt at zero balance. One run (QQQ) ends below zero, which a live account could never do.</li>
      <li><b>Sample size.</b> One trade per day means even 60 days is only a handful of trades. These results describe how the rules behaved, not what they will earn.</li>
      <li><b>Paper only.</b> Nothing here places real orders. Live trading stays disabled by a hard-coded kill switch.</li>
      <li><b>Tradovate API ≠ market data.</b> The $25/mo API Access add-on covers orders and account data only — real-time market data through the API is a separate CME-licensed arrangement (demo uses simulated data). A live version needs its data feed sorted separately.</li>
    </ul>
    <p class="muted" style="margin-bottom:2px">Data notes from the fetch:</p>
    <ul>{data_notes}</ul>
  </div>

  <footer>Generated {html.escape(summary['generated_at'])} · {html.escape(summary['execution_model'])}</footer>
</div>
</body>
</html>
"""
    out = ROOT / "site" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"Wrote {out} ({len(page):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
