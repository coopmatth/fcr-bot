# ORB-15 — Frozen Strategy Specification

**Identity.** This document is the strategy. ORB-15 is exactly the
rules below, as implemented by `ORBStrategy(minutes=15)` in
`fcr/strategies_intraday.py` (`STRATEGY_FACTORIES["orb15"]`) with the
SPY `SymbolMeta` from `run_strategies_intraday.py`. Any change to any
rule here creates a *different* strategy, not a variant of ORB-15.
Any backtest or forward test claiming to be ORB-15 must implement
this document exactly.

Frozen: 2026-10-21, after the Polygon (SIP) arbiter run CONFIRMED
the strategy (see Validation record).

## Instrument and data

- Instrument: SPY shares. Point value $1.00 per share per $1.00 of
  price movement; tick $0.01; no unit cap.
- Bars: 1-minute, regular trading hours only — bars stamped
  09:30 ≤ t < 16:00 America/New_York.
- Paper account: $500 starting equity, tracked across the whole run.
  The simulator never halts for balance or margin; equity is
  bookkeeping. Realized-P&L accounting only (no mark-to-market).

## Rules

1. **Opening range.** The high and low of the bars stamped
   09:30 ≤ t < 09:45 ET (the first 15 minutes). The range finalizes
   when the first bar stamped ≥ 09:45 arrives. If a day has no bars
   inside the opening window, there is no trade that day.
2. **Entry trigger.** The first bar stamped before 12:00 ET whose
   CLOSE is beyond the range: close > range high → long; close <
   range low → short. A wick beyond the range without a close beyond
   it does not trigger. The entry fills at the trigger bar's close.
3. **Stop.** The opposite edge of the opening range (long → range
   low; short → range high).
4. **Target.** Entry ± 2R, where R = |entry − stop| (long: entry +
   2R; short: entry − 2R).
5. **One trade per day.** The first trigger is the day's only
   signal. If it cannot be taken (rules 6–7), the day is over —
   there is no second attempt.
6. **Sizing.** Shares = floor($150 / |entry − stop|) — a $150 risk
   budget per trade, uncapped share count. Zero shares means the
   stop is too wide for the budget: the signal is counted
   skipped-for-size and the day ends without a trade.
7. **Degenerate geometry.** If the stop (or target) is not strictly
   on the correct side of the entry price, the signal is void and
   the day ends without a trade.
8. **Management.** Starts on the bar AFTER the entry bar. On each
   bar, in this order: if the bar touches the stop, exit at the
   stop price (reason STOP); else if it touches the target, exit
   at the target price (reason TARGET). A bar touching both exits
   at the stop. Exits fill exactly at the stop/target price — gaps
   through a level do not improve or worsen the fill.
9. **Flatten.** A position still open when the 15:55 bar arrives
   exits at that bar's OPEN (reason EOD_FLATTEN), before any
   stop/target check on that bar. If the data ends with a position
   open, the runner force-closes at the last bar's close
   (reason DATA_END).
10. **P&L.** pnl = ±(exit − entry) × shares × $1.00. R multiple =
    pnl / (risk distance × shares × $1.00), so a stop exit is
    −1R and a target exit is +2R by construction.

## Friction model

Gross numbers model **no** slippage, spread, or commissions.
After-friction ("AF") numbers deduct shares × **$0.04/share round
trip** per trade. AF is the basis for every verdict below.

## Frozen validation criteria

Applied identically on both tapes, window **2024-10-21 →
2026-10-19** (500 trading days; H1 = 2024-10-21 → 2025-10-20,
H2 = 2025-10-21 → 2026-10-19):

- (a) 24-month net-after-friction > $0
- (b) H2 net-after-friction > $0
- (c) after-friction realized-equity trough > $0

## Validation record

| | Polygon (SIP) — arbiter | Databento (DBEQ.BASIC) |
|---|---|---|
| Trades (W/L) | 498 (214/284) | 498 |
| Win rate (gross) | 43.0% | — |
| Gross | +$8,608.80 (+57.81R) | — |
| **Net after friction** | **+$6,803.40** | **+$7,157.36** |
| H1 AF | +$5,674.36 | +$6,979.37 |
| H2 AF | +$1,129.04 | +$177.99 |
| AF trough | $339.98 (2024-10-21 10:31) | $340.81 |
| Max drawdown (gross) | $1,738.37 | — |
| Skipped for size | 0 | 0 |
| Frozen test | **PASS (a ✓, b ✓, c ✓)** | PASS, but single-sample |

**Verdict: CONFIRMED.** The Databento SPY feed is a sampled,
non-reproducible fraction of the market (its two pulls disagreed);
Polygon's consolidated tape is deterministic and is the arbiter.
ORB-15 passes on the arbiter tape at a similar magnitude, so the
edge is not a feed artifact. Full records:
`results/polygon_verdict.json`, `results/orb15_spy_poly.json`,
trade log `results/trades_orb15_spy_poly.csv`.

## Robustness appendix (2026-10-21, `run_orb15_deepdive.py`)

Pre-declared stress tests; the spec above was not changed.

- **Friction 2× ($0.08/sh):** AF +$4,998.00, H2 +$350.88, trough
  $329.82 → PASS. **Friction 3× ($0.12/sh):** AF +$3,192.60 but
  H2 −$427.28 → FAIL on criterion (b) only. The edge survives
  double the modeled friction; at triple, the weaker second half
  goes under.
- **Execution lag (fill at next bar's open, signal-time levels):**
  498 trades, 0 voided, AF +$6,807.14, trough $339.57 → PASS.
  The result does not depend on getting the signal bar's exact
  close.
- **Sub-periods (AF):** 2024H2-partial +$1,946.20 · 2025H1
  +$2,914.58 · 2025H2 +$1,223.57 · 2026H1 +$587.86 ·
  2026H2-partial +$131.19 — 5/5 green, but **decaying**: each of
  the last three buckets is smaller than the one before.
- **Drawdown character (AF, daily realized):** max drawdown
  $1,992.55 from the 2026-03-02 peak ($8,486.91) to 2026-07-14
  ($6,494.36); the account was still underwater at the window's
  end — 160 consecutive trading days below the peak, unrecovered.
  9 of 25 months were red; worst month 2025-09 (−$1,279.39).
- **Shape:** shorts contributed +$5,448.56 of the +$6,803.40
  (231 trades) vs longs +$1,354.84 (267 trades) at near-identical
  win rates — on this tape the short side did most of the work.
  The 10 best days produced 43.6% of the total net profit.
  Exits: 120 TARGET (+$35,267.93), 251 STOP (−$38,321.87),
  126 EOD_FLATTEN (+$9,692.89), 1 DATA_END (+$164.45).

Full diagnostics: `results/orb15_deepdive.json`.
