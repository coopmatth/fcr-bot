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
