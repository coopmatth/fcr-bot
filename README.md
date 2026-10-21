# First Candle Rule (FCR) Bot — PAPER ONLY

A paper-simulation implementation of the "First Candle Rule" strategy for
**MNQ (micro E-mini Nasdaq-100 futures)**, with a bar-by-bar state-machine
engine shared by the backtester and a demo-only Tradovate integration.

**This project places no live orders.** The backtester only simulates
trades on historical bars, and the Tradovate layer is built against the
DEMO (simulation) environment behind a hard-coded kill switch
(`LIVE_TRADING_ENABLED = False` in `tradovate/config.py`). While the
switch is off, the code refuses to select the live Tradovate host and the
execution adapter refuses every environment except demo.

## Current configuration (config.yaml)

| Setting | Value |
| --- | --- |
| Instrument | MNQ — micro E-mini Nasdaq-100 futures |
| Point value | **$2.00 per index point per contract** |
| Paper account | **$500** starting balance (equity tracked across the backtest) |
| Risk budget | **$150 per trade** |
| Position size | `contracts = min(5, floor(150 / (stop distance × $2)))`; 0 → trade skipped |
| Max contracts | **5** |
| Reward:risk | Fixed **3:1** (target = entry ± 3 × stop distance) |
| Trades per day | **One, maximum** (hard lock) |
| Session | Regular hours only, 9:30–16:00 ET |

The sizing code is instrument-aware (point value + unit cap come from
config); the original SPY share-based configuration still works by
setting `instrument_type: equity`, `point_value_usd: 1.0` and omitting
`max_contracts`.

## The rules, exactly as implemented

All times America/New_York.

1. **Levels.** The first 5-minute candle of the RTH session (9:30–9:35)
   sets the day's only two levels: its **high** and its **low**.
2. **Break + displacement.** After 9:35, wait for price to break one
   level *with* an FVG (fair value gap) in the breaking move:
   - Bullish FVG (supports a long): `bar[i-2].high < bar[i].low`.
   - Bearish FVG (supports a short): `bar[i-2].low > bar[i].high`.
   - The break counts only if the displacement candle (middle bar) or
     the confirming candle **closes** beyond the level.
3. **Retest.** After a break+FVG, wait for a later bar whose range
   **overlaps the FVG zone**.
4. **Entry trigger.** On a retest bar, enter at that bar's **close** if
   it is an engulfing candle in the setup direction (vs. the immediately
   preceding bar).
5. **Stop / target.** Stop = **far edge of the FVG**. Target = entry ±
   **3.0 × |entry − stop|**. Because R is measured against actual
   dollars risked after contract flooring, a target hit is exactly
   +3.00R and a stop-out exactly −1.00R.
6. **One trade per day — hard rule.** Once a position opens the engine
   can never arm another setup that day; once it closes, the day is
   DONE.
7. **Cutoffs.** No entries at/after **12:00 ET**. Any open position is
   **flattened at 15:55 ET** (at that bar's open). If one bar touches
   both stop and target, the stop is assumed first (conservative).

Engine states: `WAIT_FIRST_CANDLE → LEVELS_SET → WAIT_BREAK →
WAIT_RETEST → IN_TRADE → DONE` (`LEVELS_SET` is transient).

The native bar size is **1 minute**. The 60-day Yahoo functional
backtest (further below) feeds **5-minute** bars instead — free Yahoo
1-minute history only goes back ~7 days — so in that run the first RTH
5-minute bar is the first candle and all patterns form on 5-minute
bars. That is a deliberate, documented deviation, not the production
configuration. The Databento run directly below is the true
configuration on real 1-minute bars.

## Backtest results — true 1-minute configuration (Databento, fetched 2026-10-21)

Data: Databento **GLBX.MDP3**, symbol **MNQ.v.0** (continuous front
month, volume roll), schema **ohlcv-1m**, 2026-08-10 → 2026-10-20 ET —
71,520 one-minute bars covering **52 RTH trading days**, downloaded
for **$0.26** against signup credit. (The raw CSV is not in this repo;
re-fetch with `fetch_databento.py`.)

Same execution model as below: fills at the engulfing bar's close,
**no slippage, spread, or commissions modeled**, and the simulator
never halts for balance or margin.

| Run | Data | Days | Trades | Win rate | Net $ | Net R | End equity | Max DD | Avg contracts | Skipped (size) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MNQ 1m, 60d | MNQ.v.0, 2026-08-10 → 2026-10-20 | 52 | 13 | 15.4% (2W/11L) | −$374.50 | −5.00R | $125.50 | $881.50 | 3.15 | 1 |

Honest reading: the true 1-minute configuration also lost money over
this window — at a 3:1 payout it needs a >25% win rate to break even
and produced 15.4%, with both winners (+$432 and +$408, both shorts
reaching the full +3R target) outnumbered by eleven −1R stop-outs.
The simulated equity also dipped negative (trough −$282.50 on
2026-10-09) because the simulator ignores margin: a real $500 account
would have been stopped out by its broker around late September and
never taken the last two trades. So even under the simulator's
generous accounting the $500 account lost three quarters of its value;
the rules as coded do not show an edge on MNQ over Aug–Oct 2026.
Trade log: `results/trades_mnq_1m_databento_60d.csv`.

## Experiment matrix — 6 variants × 5 symbols (Databento, fetched 2026-10-21)

Follow-up question: do principled adjustments make the rule
profitable, and on which markets? Five 1-minute Databento series over
the same window (2026-08-10 → 2026-10-20; SPY ends 2026-10-19, one
day shorter — DBEQ.BASIC lags a day): **MNQ** (MNQ.v.0), **MES** priced
off the ES.v.0 tape, **MGC** off GC.v.0, **SIL** off SI.v.0 — futures
use the full-size contract's tape, sized as the micro ($2/$5/$10/$1000
per point) — and **SPY** shares (DBEQ.BASIC). The ES/GC/SI/SPY pulls
cost ≈ **$1 total** against signup credit. Every run uses the common
spec: $500 start, $150 risk budget, futures ≤5 contracts, SPY uncapped
shares, one trade/day, entries before 12:00 ET, flat 15:55 ET, no
slippage/fees modeled.

Variants (frozen definitions, identical on all five symbols):

- **V0 baseline** — the original rules exactly as shipped.
- **V1 min-FVG** — setup valid only if the FVG height ≥ 0.5 × ATR(14)
  of the day's RTH 1-minute bars before the displacement candle
  (fewer than 5 bars → setup invalid).
- **V2 first-candle filter** — skip the day if the first-candle range
  exceeds 1.5 × the median first-candle range of all prior days in the
  dataset (needs ≥5 prior days).
- **V3 partials + BE** — at +1.5R bank half the position (floor; a
  1-unit position banks nothing) at that price and move the stop to
  breakeven; the remainder exits at +3R, the moved stop, or 15:55.
- **V4 trend** — setups only in the direction of the first candle's
  body (close vs open; flat body allows both).
- **V5 combined** — V1+V2+V3+V4.

| Symbol | V0 baseline | V1 min-FVG | V2 FC filter | V3 partials | V4 trend | V5 combined |
| --- | --- | --- | --- | --- | --- | --- |
| MNQ | 13t · 15% · −$374.50 · −5.00R | 19t · 5% · −$1,751.50 · −15.00R | 11t · 9% · −$696.50 · −7.00R | 13t · 31% · −$290.00 · −3.40R | 10t · 20% · −$92.00 · −2.00R | 14t · 7% · −$1,533.00 · −12.25R |
| MES | 23t · 22% · −$358.75 · −3.00R | 21t · 33% · **+$185.00 · +4.72R** | 22t · 23% · −$215.00 · −2.00R | 23t · 30% · −$589.38 · −8.10R | 13t · 8% · −$840.00 · −9.00R | 12t · 50% · −$126.25 · +1.10R |
| MGC | 18t · 28% · **+$584.00 · +2.00R** | 22t · 18% · −$430.00 · −6.00R | 16t · 25% · **+$299.00 · +0.00R** | 18t · 33% · **+$447.50 · +0.80R** | 11t · 27% · **+$373.00 · +1.00R** | 12t · 33% · **+$124.50 · −0.25R** |
| SIL | 20t · 20% · −$715.00 · −4.00R | 16t · 19% · −$475.00 · −4.00R | 19t · 21% · −$575.00 · −3.00R | 20t · 30% · −$85.00 · +0.40R | 15t · 7% · −$1,365.00 · −11.00R | 12t · 42% · **+$502.50 · +4.50R** |
| SPY | 14t · 14% · −$903.93 · −6.00R | 16t · 12% · −$1,203.14 · −8.00R | 13t · 15% · −$753.97 · −5.00R | 14t · 29% · −$602.76 · −4.00R | 8t · 12% · −$600.46 · −4.00R | 8t · 25% · −$449.53 · −3.00R |

Machine-readable matrix (with ending equity, max drawdown, average
size, skipped-for-size): `results/experiments.json`; per-run trade
logs: `results/trades_<symbol>_1m_<variant>.csv`. Reproduce with
`run_experiments.py`; `verify_experiments.py` re-derives every number
from the CSVs independently (it confirms V0 MNQ still reproduces the
−$374.50 baseline trade-for-trade, and hand-checks all 41 partial
legs in the V3/V5 runs).

Honest reading: **7 of 30 cells finished positive, and every variant
lost money summed across the five symbols — no adjustment made the
rule broadly profitable.** Gold (MGC) is the only consistent bright
spot: positive in 5 of its 6 cells, including the untouched baseline —
the best single cell in the matrix. Partials (V3) reliably softened
losses (higher win rates, shallower drawdowns, e.g. SIL −$715 →
−$85) without creating an edge. Min-FVG (V1) backfired on MNQ:
rejecting a setup doesn't end the day, it hands the day to a later,
usually worse setup. SPY lost under every variant, and so did MNQ —
the market the video teaches.

**Small-sample warning.** Each cell holds only ~8–23 trades over ~60
days (roughly 13 in a typical run). At that size a few trades'
difference flips almost any cell's sign — gold's whole baseline
profit is about one extra winning trade — and picking the best of 30
cells is itself a form of overfitting: with 30 tries, chance alone
manufactures a few good-looking cells. The variant columns also share
most of their trades, so gold's 5-of-6 is not five independent
confirmations. Nothing here is a validated edge; the gold column is
a hypothesis for a longer test, not a result. (That longer test now
exists — see "Gold 24-month validation" below.)

## Gold 24-month validation (Databento, fetched 2026-10-21)

The matrix above left exactly one hypothesis standing — gold — and
this is the longer test it called for. Same frozen variant
definitions (no retuning), same $500 account / $150 risk / ≤5
contracts spec, on 24 months of true 1-minute GC bars: GC.v.0
continuous (Databento GLBX.MDP3, ohlcv-1m), 2024-10-21 → 2026-10-20,
516 RTH trading days, traded as MGC at $10/pt. The data cost $2.56
of the account's signup credit; the raw CSV stays out of the repo
(re-fetch with `fetch_databento_gold24.py`).

Pass criteria were frozen **before** the run and applied
mechanically, in the order V0 → V3 → V5; the first variant passing
all three becomes the gold bot's strategy:

- **(a)** 24-month net $ > 0 **and** net R > 0;
- **(b)** the second half (2025-10-21 → 2026-10-20) also net positive on both;
- **(c)** the simulated day-end equity trough stays above $0 (a real
  $500 account would have survived the whole run).

| Variant | Trades | Win rate | Net $ | Net R | End equity | Max DD | Trough | Worst streak | (a) | (b) | (c) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0 baseline | 224 (64W/160L) | 28.6% | **+$2,191.00** | +26.26R | $2,691.00 | $1,543.00 | $355.00 (2024-10-22) | 9 | ✓ | ✓ | ✓ |
| V3 partials+BE | 224 (94W/130L) | 42.0% | +$2,213.50 | +22.35R | $2,713.50 | $1,456.50 | $355.00 (2024-10-22) | 6 | ✓ | **✗** | ✓ |
| V5 combined | 87 (39W/48L) | 44.8% | +$1,654.00 | +12.04R | $2,154.00 | $839.00 | $379.00 (2024-11-08) | 5 | ✓ | ✓ | ✓ |

Half splits (net $ / net R): V0 — H1 +$1,454.00 / +24.20R (117
trades), H2 +$737.00 / **+2.06R** (107 trades). V3 — H1 +$1,937.00 /
+24.60R, H2 +$276.50 / **−2.25R**. V5 — H1 +$1,458.00 / +11.99R,
H2 +$196.00 / +0.05R. Full record: `results/gold24.json`; per-variant
trade logs in `results/`; independent recompute in
`verify_gold24.py` (all headline stats and half splits re-derive
from the CSVs, V0 invariants hold, and the 60-day MGC V0 run above
reproduces exactly — untouched).

**Result: V0 baseline passes all three criteria and is the bot's
strategy.** Two mechanical notes matter more than the headline.
First, V3 actually made *more dollars* than V0 — and still failed,
because criterion (b) requires the second half to be positive in R
as well as dollars, and partials shrank its recent winners below
its losers (−2.25R). The criteria were frozen precisely so this
call isn't made by feel. Second, V5 also passed everything but is
checked after V0 in the frozen order.

Honest reading: 224 trades is a real sample, and a 28.6% win rate
clears the 25% breakeven a fixed 3:1 target needs. But the edge
decayed hard — the second year contributed only +2.06R of the
+26.26R total — the max drawdown ($1,543) is three times the $500
starting balance (it arrived in year two, after equity had grown
toward ~$2,960, which is the only reason the account was never in
danger), and **no slippage, spread, or commissions are modeled**:
224 round trips of micro commissions plus a tick or two per side
would take a real bite out of +$2,191. This is a hypothesis that
survived a 24-month test — the strongest result in this repo — not
a guarantee.

Data quality: 22 of the 516 RTH days have fewer than 300 session
bars (a full session is 390). Most are scheduled early closes and
holiday sessions (Christmas Eve ×2 at 255 bars, Jul 3 / Jul 4,
Thanksgiving week, etc.). Databento separately flagged 2025-09-17,
2025-09-24 and 2025-11-28 as degraded quality; none of those three
is a short-bar day in this file, so the degradation did not remove
RTH bars. Short days were **not** dropped — every day with session
bars ran through the engine on whatever bars exist. The full list
is in `results/gold24.json`.

## Strategy Lab — eight pro tactics over 120 days (2026-10-21)

The FCR matrix asked whether *adjustments* help; the lab asks the
wider question: how do the classic professional tactics themselves
do under the same house spec ($500 paper account, $150 risk budget,
futures ≤5 contracts, equities in shares, one position at a time)?
The rule specs were frozen first and audited against the canonical
published versions — see
[`research/strategy_evidence.md`](research/strategy_evidence.md)
(Crabel for ORB, Raschke for the sweep, Connors for RSI(2), the
Turtle literature for Donchian). That audit caught one real bug,
fixed before the run: **Connors RSI(2) enters AT the signal day's
close**, but `fcr/daily.py` filled at the next open. It now fills
at the signal close (the SPY smoke trade moved from the 2026-06-11
open, 728.76, to the 2026-06-10 close, 725.43 — same exit, +$49.32
instead of +$36.00).

**Windows.** Intraday pack: 1-minute Databento bars, 2026-06-22 →
2026-10-20 — 120 *calendar* days = 87 RTH sessions (84 for SPY,
whose DBEQ.BASIC data ends 2026-10-19). Daily pair: last 120
*trading* days (2026-04-30 → 2026-10-20) on Yahoo daily bars.

**Friction.** Gross columns model no costs, as everywhere in this
repo. Every run also reports a net-of-friction twin — a fixed
per-unit round-trip charge (MNQ $3.40, MES $4.90, MGC $4.40, SIL
$12.40, SPY/QQQ $0.04/share: micro fees plus ~1 tick of slippage
per side) — and the lab ranks by the after-friction number. It
matters: several gross-green cells flip red after friction
(previous-day sweep on MNQ +$216.50 → −$45.30; VWAP reversion on
MNQ +$363.11 → −$279.49; ORB close-exit on MES +$428.75 → −$95.55).

Top of the table (45 runs; full record:
`results/strategy_lab.json`, indepently spot-verified from the
trade CSVs by `verify_strategy_lab.py`):

| Run | Trades | Net $ (gross) | Net after friction | Net R |
| --- | --- | --- | --- | --- |
| Initial Balance breakout — SPY | 68 | +$1,385.14 | **+$1,115.58** | +9.24R |
| FCR V0 baseline — SPY | 26 | +$1,496.26 | **+$1,001.62** | +10.00R |
| VWAP trend pullback — SPY | 45 | +$1,638.15 | **+$829.15** | +10.96R |
| Previous-day sweep — MGC | 29 | +$1,153.00 | **+$726.20** | +5.76R |
| ORB 15m — SPY | 83 | +$754.35 | **+$455.07** | +5.05R |
| Initial Balance breakout — MES | 64 | +$728.77 | **+$224.07** | +5.61R |

By net R the order shuffles (VWAP trend SPY +10.96R first, FCR SPY
+10.00R second) but the same names stay on top. The bottom is
dominated by VWAP ±2σ reversion, which lost on all five symbols
after friction (worst: MGC −$4,396.96 over 67 trades) — fading
2-sigma stretches in a trending summer/autumn was a systematically
bad idea in this window. The daily pair barely traded: Donchian
5 / 5 / 0 trades (SPY −$200.86, QQQ −$31.72 after friction; gold
produced 10 channel breakouts but every one was skipped for size —
a 2×ATR stop on GC busts the $150 budget at ≤5 MGC contracts),
RSI(2) 1 / 4 trades (SPY +$49.16, QQQ +$36.32, after friction).

**Conjunction portfolio** (`build_portfolio.py`; record:
`results/portfolio.json`, trades:
`results/trades_portfolio_combined.csv`). Top 3 cells by
after-friction $ with ≥15 trades and at most one per strategy
family — Initial Balance SPY, FCR SPY, VWAP trend SPY — traded in
one shared $500 account under a $300 cap on summed open initial
risk: 138 of 139 trades taken (one FCR trade skipped by the cap),
combined **+$2,516.35** after friction, 42.0% win rate, ending
equity $3,016.35, realized-curve max drawdown $3,014.19 — versus a
naive uncapped sum of +$2,946.35. All three components trade SPY
intraday, so the diversification is by strategy logic, not by
market; the equity curve is realized-P&L-only (no mark-to-market),
which the portfolio record states explicitly.

**Honest reading.** 13 of 45 cells finished green after friction —
but with 45 cells on test, several green cells are expected by
chance alone, and 5 of the 13 rest on fewer than 15 trades
(†-marked on the dashboard; the two SIL ORB cells have exactly one
trade each). The credible standouts are the ≥15-trade green cells,
and they cluster on SPY in a single Jun–Oct 2026 window — one
market, one regime. They have earned a longer-window test, not
belief. As always here, the simulator never halts for balance or
margin; several runs' paper equity goes deeply negative, which a
real $500 account could never survive.

**Correction (added 2026-10-21, from the 24-month validation
below).** The SPY intraday data behind every SPY cell in this lab
(Databento DBEQ.BASIC) turned out to be **non-reproducible across
downloads** — a second pull of the same history disagrees with the
lab's file on roughly a third of overlapping bars. The SPY cells
above are therefore **single-sample results**: exact for the one
file that produced them (the engines re-verify perfectly on that
file), but not properties of a canonical SPY tape. See the
validation section next for what survives that caveat.

## Winners — 24-month validation (2026-10-21)

The lab's top five cells re-run over 24 months (2024-10-21 →
2026-10-20; the SPY file ends 2026-10-19), same frozen spec and
friction model. Frozen pass test: (a) 24-month net-after-friction
> $0, (b) second half (2025-10-21 → end) net-after-friction > $0,
(c) after-friction realized-equity trough > $0. Full record:
`results/validation24.json`; harness: `run_validation24.py` +
`run_validation24_spy.py`.

**Previous-day sweep — MGC: FAIL.** The futures file is
deterministic (GLBX.MDP3), so this verdict is solid. Over 24
months: 184 trades (67W/117L), gross +$1,168.00 (+8.60R) but
**−$1,564.40 after friction**; halves after friction H1
−$1,735.80 / H2 +$171.40; after-friction trough **−$2,116.20**
(2026-08-20). The $150-risk micro sizing pays $4.40/contract in
friction against a thin gross edge — the lab's +$726.20 was one
favourable 120-day window, not an edge. Fails (a) and (c).

**SPY data integrity.** DBEQ.BASIC SPY 1-minute bars are not
reproducible across pulls: `spy_1m_120d.csv` (lab file) vs
`spy_1m_24mo.csv` (validation file) share identical timestamps and
bar counts (37,015 overlap bars) yet disagree on **10,234 bars**
(10,202 in price, median |Δclose| $0.11, max $6.66), and a third
pull disagrees with both. The pattern (single-print bars in one
pull where another shows full consolidated bars) points to the
sampled construction of the BASIC feed. The gated harness
therefore refused to validate SPY by slice-matching; the code
route itself is proven exact (all four SPY cells re-run on the
original lab file reproduce the lab to the cent). The honest
completion is a **single-file** validation on
`spy_1m_24mo.csv`, with a cross-sample replication check reported
next to every verdict:

| Cell (lab window) | Lab file: trades / gross / AF | New file: trades / gross / AF | Stability |
| --- | --- | --- | --- |
| IB breakout SPY | 68 / +$1,385.14 / +$1,115.58 | 68 / +$1,484.55 / +$1,216.55 | STABLE |
| FCR V0 SPY | 26 / +$1,496.26 / +$1,001.62 | 32 / −$604.15 / −$1,281.03 | **FLIPPED** |
| VWAP trend SPY | 45 / +$1,638.15 / +$829.15 | 40 / +$1,024.44 / +$409.36 | STABLE |
| ORB 15m SPY | 83 / +$754.35 / +$455.07 | 83 / −$164.84 / −$462.32 | **FLIPPED** |

**SPY full 24 months, single file** (500 trading days):

| Cell | Trades | W/L | Gross $ | Net R | After friction | H1 AF | H2 AF | Trough (AF) | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| IB breakout | 415 | 185/230 | +$6,872.31 | +46.13R | **+$5,119.23** | +$3,431.22 | +$1,688.01 | $500.00 (never below start) | **PASS** (STABLE) |
| FCR V0 | 171 | 48/123 | +$3,231.21 | +21.55R | −$181.55 | +$965.51 | −$1,147.06 | $35.81 | FAIL (a, b) — FLIPPED |
| VWAP trend | 272 | 111/161 | +$8,622.05 | +57.59R | **+$4,013.33** | +$1,235.43 | +$2,777.90 | −$217.56 | FAIL (c only) — STABLE |
| ORB 15m | 498 | 213/285 | +$9,097.52 | +60.91R | **+$7,157.36** | +$6,979.37 | +$177.99 | $340.81 | **PASS (single-sample, unstable)** — FLIPPED |

Reading them straight: IB breakout is the one clean survivor —
positive after friction over 24 months, positive in both halves,
never underwater, and sign-stable across the two data samples.
VWAP trend makes real money on this file and is sign-stable, but
its realized equity dipped to −$217.56 in June 2025, so on a real
$500 account it dies mid-run: FAIL on the survivability criterion
alone. ORB 15m posts the biggest dollars but flipped sign across
samples on the lab window and its H2 is nearly flat (+$177.99) —
a pass printed on one download, not a validated edge. FCR V0 on
SPY is negative after friction over 24 months and flipped across
samples: the lab cell does not survive.

**Portfolio (IB + FCR + VWAP trend on SPY, 24 months, new
file):** 847 of 858 trades taken (11 skipped by the $300
open-risk cap), combined **+$8,150.33** after friction, 39.9%
win rate, ending equity $8,650.33, realized-curve max drawdown
$4,889.80, trough $500.00 — PASS on its test (net > 0 and trough
> 0), versus a naive uncapped sum of +$8,951.01. It inherits
every caveat above: realized-P&L-only curve, a component that
fails standalone on survivability, and single-sample SPY data
throughout.

**Bottom line of the validation.** Of the lab's five winners,
exactly one (IB breakout on SPY) passes all three frozen criteria
*and* is stable across data samples — and even it rests on a
single DBEQ.BASIC download, so "validated" here means "survived
24 months on one internally consistent sample," a weaker claim
than the gold FCR pass, which sits on deterministic futures data.
The sweep-on-gold result is dead after friction. Nothing here
changes the paper-only status of the project.

### MES cross-check (2026-10-21)

The two credible SPY cells — IB breakout (the one clean PASS) and
VWAP trend (STABLE, failed only on survivability) — both rest on
the non-reproducible DBEQ.BASIC tape. The cross-check re-runs them
on MES: the same index on the ES.v.0 tape (GLBX.MDP3,
deterministic), traded as MES at $5/pt with the lab's
$4.90/contract friction, over the same 24 months. Harness:
`run_crosscheck_mes.py`; full record: `results/crosscheck_mes.json`.

**Slice verification: exact.** Restricted to the lab window on
the new 24-month ES file, both cells reproduce the lab to the
cent (IB breakout MES: 64 trades, +$728.77 / +$224.07 AF; VWAP
trend MES: 47 trades, −$1,313.94 / −$2,210.64 AF) — the
deterministic tape behaves exactly as the SPY tape did not.

**Full 24 months on MES (515 trading days): both FAIL.**

| Cell | Trades | W/L | Gross $ | Net R | After friction | H1 AF | H2 AF | Trough (AF) | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| IB breakout MES | 371 | 162/209 | +$3,040.01 | +28.70R | **−$267.49** | −$396.84 | +$129.35 | −$1,441.93 | FAIL (a, c) |
| VWAP trend MES | 272 | 106/166 | +$5,088.63 | +43.13R | **−$105.37** | +$780.70 | −$886.07 | −$14.16 | FAIL (a, b, c) |

Both strategies are positive *gross* on MES over two years — the
directional patterns are not SPY-feed artifacts — but at micro
size the friction bill ($3,307.50 for IB breakout, $5,194.00 for
VWAP trend at $4.90/contract) exceeds the gross edge, so both end
negative after costs, and both realized-equity curves spent time
underwater. VWAP trend misses all three criteria narrowly
(−$105.37 overall, trough −$14.16); IB breakout's recent half is
positive (+$129.35) but its first half dug a −$1,441.93 trough.
The mini-portfolio was **not built**: the frozen rule builds it
only if both cells pass.

**Framing.** MES and SPY are the same index, but ES futures
trade nearly around the clock, so the 9:30 ET "open" for MES is a
continuation of overnight trading rather than SPY's auction open
— a strategy keyed to the cash open can legitimately behave
differently on the two, so a MES failure weakens but does not by
itself falsify the SPY result, and a MES pass would have been
strong feed-independent confirmation. What the cross-check does
establish: on a tape nobody has to take on faith, both patterns
show a genuine gross edge over 24 months — and at $150 risk /
≤5 micro contracts, that edge is smaller than the cost of
harvesting it. Data note: the GLBX degraded-day flags
(2025-09-17, 2025-09-24, 2025-11-28) apply dataset-wide; those
days are retained in full in the ES file, nothing dropped.

## SPY final verdict — Polygon (SIP) tape (2026-10-21)

The validation above ends with every SPY verdict resting on
Databento's DBEQ.BASIC feed — a feed the validation itself proved
is sampled and non-reproducible. The arbiter for that problem was
always the same: re-run the four SPY cells on a consolidated
tape. That run is now done, on Polygon.io's SIP 1-minute
aggregates (`data/spy_1m_polygon.csv`, pulled through the
`custom.polygon` connector with `adjusted=false` — raw prices,
the project's convention; harness `run_polygon_spy.py`, converter
`fetch_polygon_spy.py`, full record `results/polygon_verdict.json`).

**Tape verification, before any strategy number was trusted.**
(1) *Consolidation:* SPY really trades 40–90M shares a day, and
Polygon's daily sums land there — 93.4M (2025-03-10), 57.9M
(2026-06-15), 42.1M (2026-10-15) — versus the Databento files'
~2M a day (25–39× less). The DBEQ.BASIC volumes are a sampled
fraction of the market; Polygon's are the market. (2)
*Determinism:* an independent re-pull of 2026-06-15 → 2026-06-19
returned 3,644 bars identical to the main pull, 0 differing in
OHLCV — the property DBEQ.BASIC lacked. (3) *Distance between
the tapes:* on 194,084 shared RTH timestamps, 84.6% differ in
close, but the median difference is only $0.025 (the two
Databento pulls differed by a median $0.11): a sampled feed's
minute "close" is effectively a random print inside the minute,
where the SIP close is the actual last trade. Cent-level
differences — with thousand-dollar consequences below.

**The four cells, same 24 months (2024-10-21 → 2026-10-19),
same frozen spec, same $0.04/share friction, same pass test:**

| Cell | Trades | W/L | WR | Gross $ | Net R | After friction | H1 AF | H2 AF | Trough (AF) | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| IB breakout SPY | 413 | 172/241 | 41.6% | +$2,350.87 | +15.71R | **+$644.31** | −$190.97 | +$835.28 | −$1,785.72 (2026-04-10) | FAIL (c) |
| FCR V0 SPY | 191 | 49/142 | 25.7% | +$550.20 | +3.64R | **−$3,072.80** | +$2,805.93 | −$5,878.73 | −$3,370.02 (2026-08-31) | FAIL (a, b, c) |
| VWAP trend SPY | 263 | 100/163 | 38.0% | +$4,727.26 | +31.55R | **+$999.10** | +$874.81 | +$124.29 | +$171.81 (2024-10-25) | PASS |
| ORB-15 SPY | 498 | 214/284 | 43.0% | +$8,608.80 | +57.81R | **+$6,803.40** | +$5,674.36 | +$1,129.04 | +$339.98 (2024-10-21) | PASS |

**Three-sample verdict** — a cell is CONFIRMED only if it
passes the frozen test on Polygon AND was positive after
friction on the Databento 24mo file:

| Strategy | Databento AF | Databento verdict | Polygon AF | Polygon verdict | Final call |
| --- | --- | --- | --- | --- | --- |
| IB breakout | +$5,119.23 | PASS | +$644.31 | FAIL | **NOT CONFIRMED** |
| FCR V0 | −$181.55 | FAIL | −$3,072.80 | FAIL | **NOT CONFIRMED** |
| VWAP trend | +$4,013.33 | FAIL (trough only) | +$999.10 | PASS | **CONFIRMED** |
| ORB-15 | +$7,157.36 | PASS (single-sample, unstable) | +$6,803.40 | PASS | **CONFIRMED** |

**The headline is IB breakout's collapse.** The one clean
survivor of the Databento validation does not survive the real
tape: +$644.31 after friction instead of +$5,119.23, a negative
first half, and a −$1,785.72 realized trough that would have
wiped out a real $500 account in April 2026. Its Databento edge
was substantially an artifact of the sampled feed. Conversely,
ORB-15 — dismissed above as "PASS (single-sample, unstable)"
because the two Databento pulls flipped its sign in the small
120-day window — passes at nearly the same magnitude on both
tapes over 24 months (+$6,803.40 vs +$7,157.36); the flip was a
small-window artifact, not a property of the strategy. VWAP
trend also confirms, more modestly: it passes every criterion
on the SIP tape, though its second half is thin (+$124.29) —
an edge, not a fortune. FCR V0 is negative after friction on
every SPY sample and is done on this symbol.

**The trio portfolio does not confirm.** IB breakout + FCR V0
+ VWAP trend in one shared $500 account ($300 open-risk cap)
on the Polygon file: 856 of 867 trades taken, **−$1,556.46**
after friction, trough −$2,382.42 (2026-10-09), realized max
drawdown $8,470.71. The Databento portfolio's +$8,150.33 PASS
was a sampled-tape artifact — FCR V0 alone drags −$3,072.80
standalone and IB breakout's contribution shrinks to +$644.31.
A portfolio rebuilt from the *confirmed* cells (ORB-15 + VWAP
trend) is a different object and would need its own frozen
test; it is not claimed here.

Costs remain the lab's friction model ($0.04/share round trip,
no additional slippage modeled). The raw Polygon JSONL and CSV
stay local like all data files; the code and results are in
this repo.

## ORB-15 focus — deep dive on the confirmed lead (2026-10-21)

With ORB-15 the confirmed SPY lead, it got a dedicated workup
(`run_orb15_deepdive.py`, results in `results/orb15_deepdive.json`;
dashboard section "ORB-15 deep dive"). The strategy's rules are
now frozen as its identity document: **`ORB15_SPEC.md`** — any
future forward test must match it exactly.

Diagnostics on the 498-trade Polygon log (after-friction basis —
a "win" is AF > 0): 16 of 25 months green; the short side
produced +$5,448.56 of the +$6,803.40 (231 trades) vs longs
+$1,354.84 (267 trades) at near-identical win rates; exits were
120 TARGET (+$35,267.93), 251 STOP (−$38,321.87), 126
EOD_FLATTEN (+$9,692.89, 73% AF win rate), 1 DATA_END. Payoffs
are mechanical (±2R/−1R of ~$150 risk): largest win +$297.57,
largest loss −$162.00, average win +$218.24 vs average loss
−$139.23; the 10 best days hold 43.6% of the total net profit.
The honest cost: max drawdown (AF, daily realized) $1,992.55
from 2026-03-02 to 2026-07-14, and the account was still
underwater after 160 consecutive trading days at the window's
end — unrecovered, and the strategy's worst stretch is its
most recent one.

Robustness (pre-declared; spec untouched): friction at 2×
($0.08/share) still **passes** (+$4,998.00, H2 +$350.88); at 3×
($0.12/share) it **fails** on the second-half criterion only
(H2 −$427.28; 24-month total +$3,192.60). A 1-bar execution lag
(fill at the next bar's open, signal-time stop/target, via the
`entry_delay_bars` stress harness in `fcr/strategies_intraday.py`)
passes at +$6,807.14 with 0 voided signals — the result does not
depend on capturing the signal bar's exact close. Sub-periods
are 5/5 green (2024H2-partial +$1,946.20 · 2025H1 +$2,914.58 ·
2025H2 +$1,223.57 · 2026H1 +$587.86 · 2026H2-partial +$131.19)
but decaying — each of the last three buckets is smaller than
the one before. Confirmed is not the same as strengthening.

## ORB-15 on futures (MES) — 24-month validation (2026-10-21)

<!-- ORB15-MES-VALIDATION:BEGIN -->
The gate for Matt's live-$500 question. ORB-15 as confirmed on
SPY risks its $150 with ~100 shares (~$77k notional) — no $500
stock account can run it (margin needs $2,000 minimum equity by
regulation, and fractional shares create no leverage). MES is
the faithful executable form at that size, so the frozen
strategy was re-run on the deterministic ES tape
(`data/es_1m_24mo.csv`, GLBX.MDP3) over the same window as the
SPY Polygon validation, 2024-10-21 → 2026-10-19, traded as MES.
Sizing — the futures convention, stated in full:
**contracts = min(5, floor($150 / (stop_points × $5/pt)))**;
0 contracts (stop wider than 30 ES points) = skipped-for-size,
day consumed; friction $4.90/contract round trip. This differs
from the SPY run's uncapped shares by design. Route verified
first: the lab-window slice on the 24-month file reproduces
`orb15_mes_120d` to the cent (74 trades, +$571.25 / +$46.95 AF).

| | MES (GLBX) | SPY (Polygon) |
|---|---|---|
| Days / trades | 514 / 429 (190W/239L) | 500 / 498 (214W/284L) |
| Win rate | 44.3% | 43.0% |
| Gross | +$6,340.00 (+55.76R) | +$8,608.80 (+57.81R) |
| **Net after friction** | **+$2,929.60** | **+$6,803.40** |
| AF per trade | +$6.83 | +$13.66 |
| H1 AF / H2 AF | +$2,443.95 / +$485.65 | +$5,674.36 / +$1,129.04 |
| AF trough | $338.00 (2024-10-21) | $339.98 (2024-10-21) |
| Skipped for size | 83 | 0 |
| Frozen test | **PASS (a ✓, b ✓, c ✓)** | PASS |

The overnight-continuation effect is real: the per-trade AF
edge on MES is 0.50× SPY's — almost exactly the halving the
ib_break cross-check predicted. Decomposition: the gross edge
only falls ~15% ($17.29 → $14.78/trade); the rest is friction
($4.90 × ~1.62 contracts ≈ $7.95/trade vs ~$3.62/trade in
shares). Unlike IB breakout, ORB-15's edge is large enough
that half of it still clears the bar. Honest caveats, same as
SPY: the decay pattern is present here too — sub-periods
2024H2-partial +$806.75 · 2025H1 +$1,692.00 · 2025H2 +$37.75 ·
2026H1 +$793.85 · 2026H2-partial **−$400.75** (the only red
bucket, and the most recent); 14 of 25 months green; shorts
again did most of the work (+$2,240.05 AF, 76% of the total,
vs longs +$689.55). Risk shape: stop distances 3.5–30 points
(median 16.5); contract counts 1×251, 2×110, 3×54, 4×7, 5×7 —
a live $500 account mostly trades 1–2 contracts of this.
Harness: `run_orb15_mes_validation.py`; full record:
`results/orb15_mes_validation.json`.
<!-- ORB15-MES-VALIDATION:END -->

## ORB-15 Alpaca paper forward test (deployed 2026-10-21; scored start Thu 2026-10-22)

The forward-test runner for the frozen ORB-15 spec lives in the
`alpaca/` package and is **deployed and armed**: it runs on the
Linux box as `orb15-alpaca-paper.service` against the Alpaca
**paper** account (keys in the box's `/home/discord/fcr-bot/.env.alpaca`,
`chmod 600`, never in the repo), with `ALPACA_RUNNER_ENABLED=true`
since 2026-10-21. Everything is covered by mocked tests (140,
including the live-frontier gate tests below) plus a dry-run
replay.

**How it works.** Decisions come from the very `ORBStrategy` class
the backtest ran (same code, not a port), fed with completed
1-minute bars. On a signal the runner places a market order on the
Alpaca **paper** account and books the *actual* fill; exits are
runner-managed on the strategy's decision bars (stop/target/15:55
flatten, stop wins ties) and also filled at market. The paper
account's $100k is fill machinery only — the runner keeps its own
$500 overlay books from realized fills, sized by the frozen
$150-risk spec, and reports each trade's pnl both raw and net of
the backtest's $0.04/share friction model.

**Safety, hard-enforced.** The trading host is locked to
`https://paper-api.alpaca.markets` in code (any other host raises,
at config time and again per-request); a key ID not starting with
Alpaca's paper prefix `PK` is refused; orders place only when
`ALPACA_RUNNER_ENABLED` is explicitly true (default: observe-only —
signals are logged, nothing is ordered). Every event lands in a
JSONL log, state persists after every transition, and on restart
the runner reconciles its state against the account's actual SPY
position: any disagreement it cannot fully explain halts it (no
orders) rather than guessing. A matched position is adopted and
managed to the 15:55 flatten even if the bar feed has a gap.

**FEED NOTE — the caveat that defines what this test measures.**
Live signals come from Alpaca's **free IEX real-time feed** —
IEX only, roughly 3% of consolidated volume — while the
validating backtest used Polygon **SIP** (consolidated) bars.
IEX bars can differ from SIP bars in highs, lows and closes, so
ranges and breakout triggers may differ day to day. The forward
test therefore measures ORB-15 *as it is deployable for free*,
feed divergence included; if it disappoints, the feed is a named
suspect. The bar source is an injectable interface (`BarSource`)
so a Polygon-SIP source can replace the IEX source later without
touching strategy or runner logic.

**Proof the runner is the frozen strategy.** `run_orb15_alpaca_replay.py`
drives the runner over 25 Polygon-tape days (first 20 trading days
+ the 5 biggest-|pnl| days) through a fill-at-market simulated
broker and compares decisions with the backtest log
(`results/orb15_alpaca_replay.json`): **125/125 decision fields
match (100%)** — trade day, entry time, direction, shares, exit
reason. Exit *prices* differ by construction (backtest fills at
the exact level / 15:55 open; the runner fills at the deciding
bar's close): observed deltas −$0.54…+$0.29 per share-price unit
on the sampled days. Live fills will differ by real slippage —
that is the quantity the forward test exists to measure.

**Deploy-day artifact (2026-10-21) — the catch-up bug, its fix,
and the scoring ruling.** The service was first started at
14:32 ET on 2026-10-21, mid-session. On a fresh start the runner
backfills the day's bars and replays them through the strategy to
rebuild state — and the un-gated runner then *executed* the
replayed decisions with live paper market orders at current
prices: the morning's 09:47 long signal (strategy entry 771.25,
stop 769.755, target 774.24, 100 sh) was bought at the 14:32
market price (774.27) and immediately closed as a STOP at
774.25, booking **−$2.00** to the overlay. The strategy's true
replayed outcome for that trade (−$149.50 gross, −$153.50 net of
the friction model) was not booked; two live fills 2¢ apart were
booked instead. The CSV replay tests could not catch this — the
simulated broker fills at bar closes, so replayed executions look
identical to live ones there.

**The fix — a live-frontier gate.** `serve()` hands each day run
a `live_since` frontier (the moment the runner began consuming
the day live). Bars already complete at the frontier are
start-of-day catch-up: the strategy still replays them, but their
signals are history and can never place an order — the check
lives in the entry placement itself (`_place_entry_order`), so
no code path can bypass it. A day whose entry fired during
catch-up resolves as a **`missed_day`**: the replayed outcome is
logged for the record (closed-in-replay, or still-open-at-frontier
marked at the last catch-up close), **nothing is booked**, and the
runner never enters late. Exits are never gated — a position
adopted at restart is real and is managed/flattened normally
(the restart reconciliation path is unchanged). A pre-session
start is unaffected (catch-up is empty). The catch-up mode is
explicit in the persisted state (`live_since`, `catchup_bars`,
`missed_day`). New tests cover a resolved mid-day start, a
still-open-at-frontier start, a live-bar signal after a mid-day
start, a pre-open start, restart adoption under the gate, and
the placement-level refusal.

**Ruling.** 2026-10-21 is a **deploy-day artifact and is
excluded from forward-test scoring** (annotated in the box's
`alpaca_state/events.jsonl` as a `deploy_day_artifact` event; the
original events are kept). The overlay accounting was reset by
an explicit, recorded state adjustment, so **the scored forward
test starts Thu 2026-10-22 at exactly $500.00**.

## Backtest results (free Yahoo data, fetched 2026-10-21)

Execution model: entries fill at the engulfing bar's close; exits at
stop/target. **No slippage, spread, or commissions are modeled.** The
simulator tracks equity from $500 but never halts for balance or
margin — read negative-equity outcomes as strategy behavior, not an
achievable account history.

| Run | Data | Days | Trades | Win rate | Net $ | Net R | End equity | Max DD | Skipped (size) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MNQ 5m, 60d | MNQ=F, 2026-08-12 → 2026-10-21 | 49 | 7 | 14.3% (1W/6L) | −$283.50 | −3.00R | $216.50 | $664.50 | 1 |
| QQQ 5m, 60d | QQQ, 2026-07-29 → 2026-10-21 | 60 | 7 | 14.3% (1W/6L) | −$514.90 | −3.46R | −$14.90 | $895.14 | 0 |
| MNQ 1m, ~7d | MNQ=F, 2026-10-14 → 2026-10-21 | 5 | 0 | — | $0.00 | 0.00R | $500.00 | $0.00 | 0 |
| QQQ 1m, ~7d | QQQ, 2026-10-13 → 2026-10-21 | 7 | 0 | 0 trades | $0.00 | 0.00R | $500.00 | $0.00 | 0 |

Honest reading: on 5-minute bars over the last ~60 days the rule lost
money on both instruments — six of seven MNQ trades stopped out at
exactly −1R before the one +3R winner. The 1-minute runs produced **no
trades at all** in the ~7 days Yahoo serves — the full chain (break +
FVG + retest + engulfing before noon) simply never completed that
week. (The Databento run above has since tested the true configuration
over 52 days; it lost money too.) With one
trade per day, even 60 days is a tiny sample; these numbers describe
how the rules behaved, not what they will earn. Full trade logs are in
`results/` locally and `results/summary.json` is the machine-readable
record (also embedded in the dashboard).

Data caveats found during the fetch (all recorded in summary.json):
Yahoo's MNQ=F 5-minute history starts later than QQQ's (49 vs 60
trading days in the "60d" window); the futures series is an
**unadjusted** continuous front-month splice, so the 2026-09-18
quarterly roll appears as a price jump between days (the strategy only
uses intraday levels, so splices neither create nor erase trades);
two close-to-open moves >1.25% occurred across the globex window;
2026-09-07 (Labor Day) has no RTH bars; the final day (2026-10-21) is
a partial session at fetch time.

## Broker: Tradovate (demo first)

Matt's pick. Key facts (verified 2026-10-21 against Tradovate/NinjaTrader's
own pages unless noted): official REST + WebSocket API with a simulation
environment (`https://demo.tradovateapi.com/v1` — the docs' examples all use
the demo host; the auth response also returns per-organization `apiHosts`,
which the client should treat as authoritative); MNQ **day margin
$100/contract** (Tradovate's own margin page; initial margin $4,658.74);
API Access is a **$25/month add-on** that requires a live funded account
with at least **$1,000** (prop/evaluation accounts are not eligible);
micro commissions on the free plan are **$0.39/contract/side** plus
exchange/clearing/NFA fees (all-in ≈ $0.95/side on MNQ — third-party
estimate; Tradovate's own all-in rate PDF was unreachable, 404, on
2026-10-21, so confirm in-account before relying on it).

**Market-data caveat (important):** the API Access add-on provides order
placement and account data — it does **not** include real-time market
data. Tradovate's docs route real-time/historical market-data API access
through becoming a CME sub-vendor and signing the CME Information License
Agreement. In the demo environment, simulated data is available for
development; a live deployment needs a separate data arrangement (or a
different data source feeding the same engine). The engine doesn't care
where bars come from — only the adapter does.

Integration status: the `tradovate/` package is **built and
unit-tested with mocked transports** (auth flow, token caching/renewal,
front-month contract resolution, 1-minute chart-bar parsing into the
engine's `Bar` type, bracket/OSO order payload construction,
position/order reads, liquidate/flatten, and kill-switch refusal —
60 tests pass across the whole repo, incl. the variant gates and the
gold runner). It has never touched the real
Tradovate API: **no account or credentials exist yet**, so nothing is
blocked except first contact.

### What Matt needs to do

1. Open a Tradovate account (the FCM is NinjaTrader Clearing, LLC).
2. When ready, fund it — the API Access add-on requires a live funded
   account (min $1,000). Funding is not needed to build or backtest.
3. Subscribe to **API Access** ($25/mo) under Application Settings →
   API Access, and generate an API key — you'll receive a **CID** and a
   **secret**, shown once. Store them in a password manager.
4. Create the key for the **DEMO environment first**; everything gets
   validated in simulation before anything else is discussed.
5. Provide the credentials via **secure entry only** (password
   manager/vault share) — **never paste them in chat**. The bot reads
   them from local environment variables named in `.env.example`
   (`TRADOVATE_DEMO_USERNAME`, `TRADOVATE_DEMO_PASSWORD`,
   `TRADOVATE_DEMO_API_KEY`, `TRADOVATE_DEMO_API_SECRET`,
   `TRADOVATE_DEMO_CID`).
6. Live trading is a **separate later decision** requiring Matt's
   explicit go. By construction the code cannot trade live while
   `LIVE_TRADING_ENABLED = False`.

## Gold bot

Built 2026-10-21, the day the validation above passed. **Status:
NOT deployed anywhere. It has never connected to the real Tradovate
API and has never placed an order** — no Tradovate account or demo
API key exists yet. It is covered by mocked unit tests
(`tests/test_gold_bot.py`) and by `--dry-run` replays against
recorded days, nothing more.

What it is: `gold_bot.py` + `config_gold.yaml` — a daily runner for
the winning variant (V0 baseline, frozen) on MGC only, wired through
the existing `tradovate/` demo layer (no second broker client):

1. Resolves the MGC front-month contract (`contract/suggest`).
2. Feeds closed 1-minute RTH bars into the same `FCREngine` the
   backtester uses (WebSocket stream via `md/getChart`, with the
   in-progress bar deduplicated to each minute's final update; or
   any bar iterable — `--bars-csv` replays a recorded day).
3. The moment the engine confirms a setup, places the identical
   trade on the **demo** account: market entry with the engine's
   stop and +3R target attached as an OSO bracket
   (`order/placeOSO`). The bracket manages stop/target broker-side.
   (V0 has no partials, so there is nothing else to manage.)
4. At the 15:55 ET bar, liquidates anything still open
   (`order/liquidatePosition`); if the bar stream dies mid-trade it
   force-closes and flattens too. One trade per day, enforced by
   the engine.

Safety, hard-enforced: the runner refuses to start if
`LIVE_TRADING_ENABLED` is not `False`, if the execution client's
base URL is not the demo host, or if the strategy config drifts
from the frozen validated spec (symbol MGC, $10/pt, $150 risk,
≤5 contracts, 3.0 RR, every variant flag off) — retuning means
re-validating first. Credentials come only from the
`TRADOVATE_DEMO_*` environment variables (`.env.example`); nothing
is read from repo files.

The winning variant it runs, in one line: V0 baseline, 24 months,
224 trades, 28.6% win rate, **+$2,191.00 / +26.26R**, ending equity
$2,691.00 on the $500 paper account, max drawdown $1,543.00, trough
$355.00 — see the validation section above for the full record and
the caveats (second-half decay, no costs modeled).

How Matt runs it on demo, once the account exists:

```bash
cd ~/workspace/fcr-bot
# 1. Credentials in the environment ONLY (from a password manager —
#    never pasted in chat, never written into this repo):
#    TRADOVATE_DEMO_USERNAME / _PASSWORD / _API_KEY / _API_SECRET / _CID
# 2. First contact: replay a recorded day against the demo account:
.venv/bin/python gold_bot.py --bars-csv data/gc_1m_24mo.csv --date 2024-10-21
# 3. A live session day (needs `pip install websocket-client`):
.venv/bin/python gold_bot.py
# No-credential rehearsal — records the orders it WOULD place:
.venv/bin/python gold_bot.py --dry-run --bars-csv data/gc_1m_24mo.csv --date 2024-10-21
```

What Matt still needs to do before step 2 can run — the same list
as the Tradovate section below, unchanged: open a Tradovate
account; fund it (≥ $1,000 — the API Access add-on requires a live
funded account); subscribe to API Access ($25/mo); generate a
**demo** API key; hand the credentials over via secure entry only
(password manager / vault), **never in chat**. And the market-data
caveat still applies: demo data is simulated; a live-money version
(not built, kill switch off) would additionally need the CME data
arrangement sorted. Live trading remains a separate decision that
this code cannot make.

## Judgment calls / assumptions

- **Break definition:** a break requires a *close* beyond the level by
  the displacement or confirming candle of the FVG triplet — a wick
  poke through the level does not count.
- **FVG window starts fresh at 9:35.** Bars inside the first candle do
  not count toward an FVG triplet.
- **Retest = zone overlap.** Any bar after the FVG forms whose range
  touches the gap zone counts — including a partial dip and including
  the very next bar. No "% of gap retraced" requirement and no setup
  expiry other than the 12:00 cutoff and the invalidation rules.
- **Double-break / invalidation.** A pending setup is **cancelled** if,
  before entry, price (a) closes beyond the *opposite* first-candle
  level, or (b) closes through the far edge of its own FVG (full fill).
  On cancellation the engine returns to `WAIT_BREAK` — the **first
  completed entry wins the day**.
- **Stop placement** is the far edge of the FVG as recorded when the
  gap formed (not the retest candle's extreme, not the first-candle
  level).
- **Degenerate / unaffordable entries are skipped:** if the engulfing
  close lands on the wrong side of the stop, the setup is void; if the
  stop distance makes even 1 contract exceed the $150 budget
  (distance > 75 MNQ points), the entry is discarded and counted as
  *skipped-for-size* (the engine may still take a later, tighter setup
  the same day).
- **Execution model (backtest):** entries fill exactly at the
  engulfing bar's close; exits exactly at stop/target; the 15:55
  flatten fills at that bar's open. **No slippage, spreads, commissions,
  or partial fills.** Data ending mid-trade force-closes at the last
  bar's close with reason `DATA_END`.
- **Equity tracking** is bookkeeping only: PnL accumulates trade to
  trade from the $500 start and max drawdown is measured on the
  day-end equity curve. There is **no margin check and no halt at zero
  balance** — the QQQ run's negative ending equity is the visible
  consequence.
- Short sales are simulated symmetrically.

## Layout

```
config.yaml            strategy parameters (MNQ, risk, sizing, times)
config_gold.yaml       FROZEN gold-bot strategy parameters (MGC, V0)
fcr/patterns.py        Bar/FVG types, FVG + engulfing detectors (pure fns)
fcr/engine.py          FCREngine state machine (pure Python, no pandas)
fcr/config.py          YAML config loader + sizing normalization
fcr/data.py            CSV loader, yfinance fetcher, synthetic generator
fcr/backtest.py        day-by-day backtest runner + equity/stats + CLI
tradovate/             DEMO-only Tradovate API layer (kill-switched)
  config.py            env-var credentials, demo/live hosts, kill switch
  client.py            REST auth (token request/cache/renewal)
  market_data.py       front-month lookup + 1m chart bars -> engine Bars
  execution.py         bracket (OSO) entries, status reads, flatten
alpaca/                PAPER-only Alpaca layer for the ORB-15 forward test
  config.py            env-var credentials, paper host lock, kill switch
  client.py            PAPER trading REST (account/orders/positions)
                       + data REST (SPY 1m bars, free IEX feed)
  runner.py            ORB-15 forward-test engine: the frozen
                       ORBStrategy decides, the runner fills at
                       market, persists events/state, reconciles
                       on restart, enforces the 15:55 flatten
run_orb15_alpaca_replay.py  runner-vs-backtest proof (25 Polygon days)
deploy/                orb15-alpaca-paper.service + install runbook
                       (deployed on the Linux box 2026-10-21)
run_backtest.py        single-run CLI entry point
run_matrix.py          the 4-run Yahoo matrix -> results/summary.json
run_experiments.py     6-variant x 5-symbol matrix -> results/experiments.json
verify_experiments.py  independent checks of the experiment matrix
run_gold24.py          gold 24-month validation -> results/gold24.json
verify_gold24.py       independent checks of the gold validation
gold_bot.py            gold daily paper runner (Tradovate DEMO only)
fetch_polygon_spy.py   Polygon JSONL -> data/spy_1m_polygon.csv (SIP tape)
run_polygon_spy.py     SPY re-check on Polygon -> results/polygon_verdict.json
ORB15_SPEC.md          ORB-15 FROZEN spec (the strategy's identity document)
run_orb15_deepdive.py  ORB-15 diagnostics + robustness -> results/orb15_deepdive.json
run_orb15_mes_validation.py  ORB-15 on MES, 24-month futures validation -> results/orb15_mes_validation.json
build_site.py          regenerates site/index.html from results/
site/index.html        static dashboard (deployed to Vercel)
tests/                 test_fcr.py (engine/sizing/equity), test_tradovate.py,
                       test_variants.py, test_gold_bot.py (gold runner),
                       test_alpaca_client.py + test_alpaca_runner.py
                       (ORB-15 forward-test layer, all mocked)
.env.example           credential env-var names (placeholders only)
data/                  cached bar data (gitignored)
results/               trade logs + summary.json (gitignored locally)
```

## How to run

Tests (140: engine math/scenarios, contract sizing & cap, skipped-for-
size, equity tracking, Tradovate mocks incl. kill-switch refusal,
variant gates, the gold runner wiring, and the Alpaca paper layer —
host lock, kill switch, full-day state-machine simulations, restart
reconciliation, live-frontier gate):

```bash
cd ~/workspace/fcr-bot
python3 -m pytest tests/ -q
```

The 4-run Yahoo matrix (needs the project venv for yfinance) and the
dashboard rebuild:

```bash
.venv/bin/python run_matrix.py     # fetches data, writes results/summary.json + trade logs
python3 build_site.py              # regenerates site/index.html
```

Single backtest (synthetic smoke data, your own CSV, or the ~7-day
Yahoo 1-minute fetch):

```bash
python3 run_backtest.py                              # synthetic, 15 days, seed 7
.venv/bin/python run_backtest.py --fetch             # real ~7d of 1m bars for config symbol
python3 run_backtest.py --csv path/to/bars_1m.csv    # columns: timestamp, open, high, low, close, volume
```

Tradovate layer: import `tradovate` and construct a `TradovateClient`
from environment variables (see `.env.example`). With no credentials
set it raises a clear `MissingCredentialsError`; with the kill switch
off it can only build demo-environment requests. There is deliberately
**no CLI that places orders**.

Alpaca layer: `.venv/bin/python run_orb15_alpaca_replay.py` replays
the runner against the Polygon tape offline (no credentials needed)
and writes `results/orb15_alpaca_replay.json`. The live runner is
`.venv/bin/python -m alpaca.runner` — it refuses to start without
`ALPACA_PAPER_*` credentials, and places no orders unless
`ALPACA_RUNNER_ENABLED=true`. See the "ORB-15 Alpaca paper forward
test" section above and `deploy/INSTALL_orb15_alpaca.md`.

## What a *real* evaluation needs

Yahoo's free data cannot validate a one-trade-per-day strategy: ~7 days
of 1-minute bars produced zero setups, and 60 days of 5-minute bars is
a different bar size than the strategy specifies. A serious test needs
months of true 1-minute MNQ bars (e.g. Databento or Polygon futures
data, or Tradovate's own history once the demo account exists), plus a
cost model (commissions + a tick or two of slippage per side). Expect
the results to be sensitive to the judgment calls above — each rule
lives in exactly one place in the engine so variants are cheap to test.
