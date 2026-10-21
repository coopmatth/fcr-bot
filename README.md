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
a hypothesis for a longer test, not a result.

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
40 tests pass across the whole repo). It has never touched the real
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
run_backtest.py        single-run CLI entry point
run_matrix.py          the 4-run Yahoo matrix -> results/summary.json
build_site.py          regenerates site/index.html from results/
site/index.html        static dashboard (deployed to Vercel)
tests/                 test_fcr.py (engine/sizing/equity), test_tradovate.py
.env.example           credential env-var names (placeholders only)
data/                  cached bar data (gitignored)
results/               trade logs + summary.json (gitignored locally)
```

## How to run

Tests (40: engine math/scenarios, contract sizing & cap, skipped-for-
size, equity tracking, Tradovate mocks incl. kill-switch refusal):

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

## What a *real* evaluation needs

Yahoo's free data cannot validate a one-trade-per-day strategy: ~7 days
of 1-minute bars produced zero setups, and 60 days of 5-minute bars is
a different bar size than the strategy specifies. A serious test needs
months of true 1-minute MNQ bars (e.g. Databento or Polygon futures
data, or Tradovate's own history once the demo account exists), plus a
cost model (commissions + a tick or two of slippage per side). Expect
the results to be sensitive to the judgment calls above — each rule
lives in exactly one place in the engine so variants are cheap to test.
