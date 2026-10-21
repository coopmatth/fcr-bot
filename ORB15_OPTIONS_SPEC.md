# ORB-15 Options — Frozen Variant Specification

**Identity.** This document is the options expression of ORB-15.
The SIGNAL is exactly the frozen ORB-15 of `ORB15_SPEC.md` — the
same `ORBStrategy(minutes=15)` from `fcr/strategies_intraday.py`,
the same SPY 1-minute bars, the same rules, no re-implementation
and no tuning. What changes is only the EXPRESSION: instead of
buying/selling SPY shares sized to a $150 risk budget, each signal
buys exactly ONE listed SPY option contract. Any change to any
rule below creates a different variant, not ORB-15 options.

Frozen: 2026-10-21, by Matt's decision: stay at a $500 bankroll →
options expression of ORB-15, paper-test FIRST on the Alpaca paper
account. No live options trading is authorized by this document.

**Validation status — read this first.** ORB-15's PASS verdicts
(`ORB15_SPEC.md`) are verdicts about the SHARES expression (and,
separately, the MES futures expression). The options expression
has NO backtest and NO validation: an option's payoff is
non-linear in the underlying (delta), decays with time (theta),
and re-prices with implied volatility, so the shares P&L does not
transfer. This variant exists to be measured by its own forward
paper test, nothing more.

## Signal layer (identical to ORB-15)

1. **Opening range.** High/low of SPY 1-minute RTH bars stamped
   09:30 ≤ t < 09:45 ET, finalized by the first bar ≥ 09:45. No
   bars inside the window → no trade that day.
2. **Entry trigger.** First bar stamped before 12:00 ET whose CLOSE
   is beyond the range: close > range high → long signal; close <
   range low → short signal. Wicks do not trigger.
3. **Stop / target (underlying levels).** Stop = the opposite range
   edge; target = entry ± 2R, R = |entry − stop|, exactly as the
   frozen strategy computes them. These levels drive the option's
   exits (below) — they are never re-derived from option prices.
4. **One signal per day.** The first trigger is the day's only
   signal. The frozen strategy's own gates apply unchanged —
   including its sizing calculation: a signal the strategy voids
   (degenerate geometry) or skips for size (stop distance wider
   than the $150 share-risk budget) produces NO options trade
   either. The strategy's share count is otherwise ignored:
   options sizing is always exactly 1 contract (rule 8).

## Expression mapping

5. **Direction → contract type.** Long signal → BUY a CALL. Short
   signal → BUY a PUT. Long options only: this variant never
   sells/writes an option, never shorts a contract, and never
   trades spreads. Maximum loss on any trade is therefore the
   premium paid, by construction.
6. **Expiry.** The NEAREST listed SPY expiry on or after today —
   the 0DTE contract when today is an expiry day, otherwise the
   next listed expiry. Same rule every day; no discretion.
7. **Strike.** The listed strike closest to the signal price
   (at-the-money). An exact tie resolves toward in-the-money
   (call → the lower strike; put → the higher strike).

## Sizing and the premium gate

8. **Exactly 1 contract** (100-share multiplier). No scaling, no
   averaging, no second contract for any reason.
9. **Premium gate.** Before entry, read the contract's quote
   snapshot (source below). If the ASK is above **$2.50 per share
   ($250 per contract)**, SKIP the day and log `premium_skip` —
   volatility is too expensive. Expected typical spend is
   $120–$220; the gate caps the absolute worst case at $250,
   which is also the trade's maximum possible loss.
10. **No blind entries.** If the snapshot is missing, carries no
    usable ask, or its quote timestamp is older than **15
    minutes** at decision time, SKIP the day (`premium_skip`,
    reason recorded). The runner never enters on a stale or
    absent quote.

## Quote source and the data caveat

Quotes come from Alpaca's options snapshot endpoint
(`GET https://data.alpaca.markets/v1beta1/options/snapshots`,
`feed=indicative`). The free indicative feed may be DELAYED
relative to real-time OPRA, which is a paid add-on; the gate
therefore reads the best snapshot available and enforces the
15-minute staleness bound above rather than assuming the quote
is live. Market orders do not depend on our quote feed to
execute — they fill against the real book at whatever premium
prevails; the gate is a cost control, not an execution input.
The booked premium is always the ACTUAL fill price reported by
Alpaca, never the snapshot ask.

## Orders and exits

11. **Entry.** A day market order to buy 1 contract, placed at
    signal time (subject to the inherited live-frontier gate and
    kill switch — see Operations). The entry carries a
    deterministic `client_order_id`, like the shares runner.
12. **Exits are driven by the UNDERLYING.** The position is sold
    at market when the frozen strategy closes its simulated
    trade: SPY touches the stop level (STOP), the target level
    (TARGET, stop wins a same-bar tie), the 15:55 ET flatten bar
    arrives (EOD_FLATTEN), or the bar data ends (DATA_END /
    enforced flatten). The runner never holds an option because
    its premium "might come back"; the underlying levels are
    the only exit logic.
13. **Exit failure handling.** If the exit market order fails,
    the runner falls back once to a whole-position close
    (DELETE /v2/positions/{contract}); if that also fails it logs
    `exit_failure_critical` and keeps retrying on subsequent bars
    and at end of day. A known position is never silently
    abandoned.

## Accounting

14. **$500 overlay, realized options P&L only.** The overlay
    starts at $500.00 and moves only by closed options trades:
    pnl = (exit premium − entry premium) × 100 × contracts, from
    ACTUAL fill prices. There is no friction model and no
    mark-to-market — the Alpaca paper account's own balance is
    fill machinery only, exactly as in the shares runner.
15. **Recorded per trade:** contract symbol, strike, expiry,
    type; signal direction and the underlying entry/stop/target
    levels; entry premium paid and total cost; exit reason,
    exit premium and total proceeds; realized pnl and the
    running overlay equity.

## Operations (inherited from the shares runner, unchanged)

16. **Paper only.** `alpaca.config` hard-locks the trading host
    to `https://paper-api.alpaca.markets` and refuses a key ID
    that does not start with the paper prefix `PK`; the client
    re-checks every URL. There is no live code path.
17. **Kill switch.** Orders place only when
    `ALPACA_RUNNER_ENABLED` is explicitly truthy (the SAME env
    file and flag as the shares runner — one switch arms both
    runners; installing this variant's service is therefore a
    deliberate act, and its unit file says so). Disabled, the
    runner logs every signal and gate decision but places no
    orders. Exits of known positions are never blocked.
18. **Live-frontier gate.** A signal produced by bars that were
    already complete when the runner started consuming the day
    is history: it is logged as a missed day (`booked: false`)
    and never traded. Only a signal on a live bar can place an
    entry order, and the check lives in the entry placement
    itself.
19. **Separate state.** This variant keeps its OWN state
    directory (`alpaca_options_state/`, env
    `ALPACA_OPTIONS_STATE_DIR`), state file, event log, and
    single-instance lock. It never shares state with the shares
    runner. Both runners share the one paper account; each
    reconciles only its own instrument (the shares runner: SPY
    shares; this runner: SPY option contracts).
20. **Reconciliation and the orphan policy.** At day start the
    runner compares its state with the account's actual SPY
    option positions:
    - Exact match (symbol + contracts) with today's state →
      adopted; the day continues.
    - Exact match with a PRIOR day's state (overnight leftover)
      → flattened immediately, booked from the recorded entry
      premium, logged, and the day continues.
    - Anything else the state cannot fully explain (unknown
      contract, quantity mismatch) → **orphan policy: the
      position is flattened, `orphan_flattened` is logged with
      full detail, and the runner HALTS for the day.** An
      unmanaged option position is never left riding.
    - State claims a position the account does not hold → halt
      (the position vanished outside the runner).
21. **One position at a time, flat overnight by construction:**
    0DTE/next-expiry contracts are always exited by rule 12 the
    same day; a position surviving to the next morning is the
    leftover case of rule 20, never a strategy hold.

## What this variant deliberately is not

- Not the shares strategy's economics: the $150-risk / 2R shape
  belongs to the underlying levels; the option converts them
  into whatever premium path the market gives. A stop exit can
  lose less than the full premium (residual value) or, late in
  the day, nearly all of it (theta). Both are the measurement.
- Not a spreads/defined-risk structure, not an income overlay,
  not a hedge. One bought contract, in or out the same day.
