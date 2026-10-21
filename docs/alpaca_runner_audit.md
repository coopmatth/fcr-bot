# Hostile Audit — ORB-15 Alpaca Paper Runner

**Scope:** `alpaca/` (config, client, runner) and its use of
`fcr/strategies_intraday.ORBStrategy`, as deployed 2026-10-21
(`orb15-alpaca-paper.service`, orders enabled on the PAPER account).
**Question under audit:** *"Are there any other potential bugs that
may trigger on a live account, that could cost us money?"*
**Method:** full adversarial read of the three modules, then
fault-injection proofs in `tests/test_alpaca_faults.py` (23 tests,
all offline). Audit only — no runner behavior was changed.
**Suite state after adding the fault tests: 163 passed** (140
pre-existing + 23 audit).

Every finding is labeled **CONFIRMED** (the unsafe behavior is
demonstrated by a named, passing fault test that asserts the unsafe
outcome — if a fix lands, that test is *supposed* to fail) or
**SUSPECTED** (code-read only).

**Cross-cutting amplifier (applies to every finding):** the runner
has *no alerting of any kind* — no email, webhook, or push anywhere
in `alpaca/`. Every failure below terminates as a crashed or halted
process whose only trace is lines in `alpaca_state/events.jsonl`
and the systemd journal on the box. On paper that costs signal
quality; on a live account it means the difference between a bad
minute and a bad week is whether a human happens to read a log.

## Executive summary

| # | Finding | Severity | Status | One-line fix |
|---|---------|----------|--------|--------------|
| F1 | Entry fill the runner never sees (timeout / process death after Alpaca filled) → orphan position + permanent halt, never flattened | **High** | CONFIRMED | Send `client_order_id` on every order; on any entry error, query orders/positions before concluding anything |
| F5 | Share sizing is uncapped: worst real day requested 300 sh ≈ **$182k notional** (364× the $500 overlay); ~10% of days > $100k | **High** | CONFIRMED | Hard `max_units` / max-notional cap in SPY_META + a pre-flight buying-power check |
| F3 | Exit order rejected at stop/flatten → uncaught crash; retry only via systemd restart replay; no escalation | Medium | CONFIRMED | Catch exit failures in-run: retry with backoff, then escalate (close via DELETE /v2/positions) and alert |
| F2 | Order stuck `partially_filled` polls out to OrderError while shares sit filled; **no cancel-order capability exists anywhere** | Medium | CONFIRMED | Treat `partially_filled` as a fill state (book filled qty); add `cancel_order` |
| F4 | Partial EXIT fill leaves the remainder with no stop/target management until end-of-day | Medium | CONFIRMED | After a partial exit, keep managing the remainder (don't mark the trade done) |
| F6 | No market-calendar awareness: early-close days flatten whenever the feed ends; the flatten order goes into a closed market | Medium | CONFIRMED | Consult `/v2/clock` / `/v2/calendar`; drive flatten from the day's actual close |
| F10 | Missing minutes inside the 09:30–09:45 range window are invisible: wrong range, wrong trades, no event | Medium | CONFIRMED | Gap detection over the range window (expected vs received bar count) + a logged warning |
| F11 | Flatten is bar-driven, not wall-clock-driven: a feed exception mid-position aborts the day with no flatten attempt | Medium | CONFIRMED | Wall-clock flatten watchdog independent of the bar loop |
| F14 | No fill-sanity check: a slipped entry silently multiplies real risk (1% slip on the worst tape day ≈ **13×** the $150 budget) | Medium | CONFIRMED | Compare fill vs signal price; log — and optionally void — entries beyond a divergence bound |
| F7 | No single-instance guard: two runners on one account trade the same day twice (2× risk) | Medium | CONFIRMED | flock/PID lock on the state dir at startup |
| F8 | Corrupt state file → `JSONDecodeError` crash-loops forever under systemd; no quarantine/recovery | Medium | CONFIRMED | On load failure, rename the bad file aside, halt loudly (exit 3), never loop |
| F9 | Bar source yields out-of-order bars unguarded (dedupe only, no monotonicity check) | Low | CONFIRMED | Drop/log bars older than the last yielded timestamp |
| F12 | `get_bars` pagination loop has no page cap: a stuck `next_page_token` hangs the fetch (and the flatten with it) | Low | CONFIRMED | Cap pages (e.g. 50) and raise past the cap |
| F13 | HTTP transport follows redirects off the locked host, credentials attached; only the original URL is host-checked | Low | CONFIRMED | Refuse redirects (NoRedirect handler, as the Databento helper already does) |
| F15 | `python -m alpaca.runner` double-executes the runner module (the journal's RuntimeWarning): two class copies in the service process | Low | CONFIRMED (benign today) | Lazy-import `.runner` in `alpaca/__init__.py`, or run the service via a plain script entry |

---

## Findings

### F1 — A fill the runner never sees becomes an unmanaged orphan, forever — CONFIRMED, High
`alpaca/client.py:215` builds the order body with **no
`client_order_id`** — the one idempotency key Alpaca offers.
`urllib_transport` (client.py:87) turns a post-POST timeout into an
`AlpacaAPIError`. In `run_day` the entry call sits inside the bar
loop with no exception handling: the error (or the process's death,
SIGTERM-style) escapes **after Alpaca filled the order but before
the runner records it** — the persisted state still says flat.
On restart, reconciliation sees account=93 / state=0 and halts
("unknown holding, refusing to guess") — correctly — but the halt
path never touches the position, the halted flag persists in state,
and every later day halts again. **The position is never flattened
by anyone.** Proof:
`test_fault_fill_then_failure_orphans_position_and_halts_forever`
(both the timeout and the process-death parametrization): after the
crash the account holds 93 SPY, two further day-runs halt, exit
attempts stay 0.
*Fix:* deterministic `client_order_id` per (day, signal); on any
entry-path exception, re-read orders + position and reconcile
before deciding the day is order-free; give the halt path a
flatten-the-orphan option once a human confirms — or at minimum an
alert (see amplifier).

### F5 — Uncapped sizing: the strategy routinely asks for ~$100k+ — CONFIRMED, High
`SPY_META` (runner.py:169) sets `max_units=None`;
`fcr/engine.py:33` `compute_units` then returns
`floor(150 / stop_distance)` unbounded. Measured over the 501
trading days of `data/spy_1m_polygon.csv` with the frozen ORB-15
rules (full table in the annex): the worst day, **2024-12-05**
(range width $0.48, stop distance $0.50), requested **300 shares at
$607.87 = $182,361 notional — 364× the $500 overlay**. 48 of 499
entry days (9.6%) request > $100k notional; the median day is
~$100k-class by construction. On the paper account ($100k equity /
$400k buying power) these fill; on a live account they fill
whenever buying power allows and are rejected only when it
doesn't. A buying-power rejection on entry propagates uncaught
(day crashes; the restart converts it to a missed day via the
frontier gate — proven in the same test). Proof:
`test_fault_sizing_is_uncapped_and_a_rejection_crashes_the_day`.
*Fix:* a hard cap (`max_units` and/or max notional as a multiple of
the overlay) treated as a spec change requiring Matt's sign-off,
plus a pre-flight buying-power check so the failure is a logged
skip, not a crash.

### F3 — Exit rejection = crash; the only retry is a process restart — CONFIRMED, Medium
Exit placement (`handle_exit_from_strategy`; the post-loop flatten
at runner.py:1096) has no error handling: an `OrderError` from
`AlpacaBroker.place_market` (runner.py:336) propagates out of
`run_day`, out of `serve()`, and kills the process. systemd
(`Restart=on-failure`, 30 s) restarts it; the day replays; the
adopted position's exit is attempted exactly once more — and fails
identically if the cause persists. No in-run retry, no fallback to
`DELETE /v2/positions/{symbol}` (which the client already wraps as
`close_position` but the runner never calls), no alert. Proof:
`test_fault_exit_rejection_at_flatten_crashes_and_retries_only_via_restart`
(exit attempts: 1 after the crash, 2 after the restart, position
open throughout).
*Fix:* catch exit errors in-run; retry a bounded number of times;
fall back to `close_position`; if all fail, halt (exit 3) with the
position explicitly flagged — and alert.

### F2 — Stuck `partially_filled` orders, and no way to cancel anything — CONFIRMED, Medium
`AlpacaBroker.place_market` (runner.py:336–363) treats only
`filled` and the final-bad set (`canceled/expired/rejected/
suspended`, runner.py:316) as terminal. A live order resting in
`partially_filled` is polled 30×1 s and then raises `OrderError`
"refusing to assume a fill" — while the partial quantity is a real
position on the account (which then feeds the F1 orphan class on
the next reconcile). Grep-verified: **no `cancel_order` exists** on
the client or broker — a stuck or erroneous live order can never
be withdrawn by this code. Proof:
`test_fault_partially_filled_status_polls_out_and_order_is_never_canceled`.
*Fix:* treat `partially_filled` past a deadline as a fill of the
filled qty (the runner already books partial fills correctly —
see SAFE list); add `DELETE /v2/orders/{id}` and cancel on
poll-timeout before raising.

### F4 — Partial exit fill: the remainder loses its stop — CONFIRMED, Medium
`_book_exit` correctly leaves a remainder position, but
`handle_exit_from_strategy` then marks the day DONE and the orphan
management block is gated on `resumed and not entry_adopted` —
false for a same-day position — so the residual shares are managed
by *nothing* until the end-of-data flatten. Proof:
`test_fault_partial_exit_fill_leaves_remainder_unmanaged_until_eod`:
40 of 93 exit at the target; a later bar smashing through the
original stop produces **no** order; the remaining 53 exit only at
end of data.
*Fix:* if an exit fill is partial, keep the runner in
IN_POSITION management for the remainder with the original levels.

### F6 — No market calendar: early closes are invisible — CONFIRMED, Medium
Nothing in `alpaca/` calls `/v2/calendar` or `/v2/clock` (grep:
zero hits; `AlpacaClient` has no such method). `serve()`
(runner.py:1247) gates on weekday + a fixed 09:20–16:10 ET window.
The strategy flattens only when a bar stamped ≥15:55 *arrives*
(`fcr/strategies_intraday.py:348`). On a 13:00 early close (e.g.
the day after Thanksgiving) no such bar exists; the position is
closed only by the runner's end-of-data fallback
(`flatten_enforced`), whose market order goes out whenever the
feed ends — into a closed market, to be rejected (→ F3's crash
path) or queued for the next open (an unintended overnight hold),
depending on Alpaca's mood. The runner cannot tell the difference
between an early close, a holiday (handled safely — see SAFE
list), and a feed outage. Proof:
`test_early_close_day_flattens_at_end_of_data_not_at_1555` +
`test_safe_holiday_with_zero_bars_completes_clean`.
*Fix:* fetch the day's calendar/clock at day start; schedule the
flatten at the actual close; treat "market closed early" as a
first-class day type.

### F10 — Range-window feed gaps silently rewrite the strategy — CONFIRMED, Medium
Neither `AlpacaBarSource` nor the runner checks bar continuity.
Proof: `test_fault_feed_gap_in_range_window_silently_distorts_range`
— with the full tape (a 09:37 bar spiking to the true high of 105)
no trade fires; delete the 09:37–09:38 bars and the same morning
produces a live entry, with no gap-related event logged. The
strategy is faithfully executing a tape that never existed.
*Fix:* count expected vs received bars for 09:30–09:45 (and the
session); log a `feed_gap` event; optionally refuse to trade a
range built from < 15 bars.

### F11 — No wall-clock flatten: exits wait for bars that may never come — CONFIRMED, Medium
All exit logic lives inside the `for bar in bars:` loop
(runner.py:821) or in the block that runs when that loop *ends
normally* (runner.py:1096). If the bar iterator raises mid-position
(data API 5xx, network cut), the exception aborts the day: no exit
attempt, no flatten — proven by
`test_fault_feed_exception_mid_position_means_no_flatten_that_run`
(position open in state and at the broker, zero exit events).
Recovery exists only via process restart + adoption, and only if
the feed comes back. A feed that returns stale-but-successful
responses delays the flatten to whenever the source gives up
(16:00 by its own clock check).
*Fix:* an independent wall-clock watchdog: at 15:55 ET, if a
position is open, flatten it regardless of bar flow.

### F14 — No fill-sanity check: slippage silently rescales risk — CONFIRMED, Medium
The runner books the actual fill (honest accounting — pnl is
computed from fill prices) but never compares `fill.price` to the
signal price, and the stop/target stay at the strategy's levels.
Proof: `test_fault_slippage_fill_books_without_any_sanity_check` —
a fill 1% through the signal on the standard geometry books
silently with a real at-stop risk of $242.73 against the $150
budget (1.62×). On the tape's tightest geometry (F5's day: $0.50
intended distance at a $608 price), the same 1% slip is ~$6.08 of
extra distance — the position's true risk would be ≈ **13×** the
intended $150. No event records the divergence.
*Fix:* emit a divergence event on every fill; define a bound
(e.g. fill worse than the stop level, or >X% through) that voids
or immediately exits the trade.

### F7 — Two instances, one account, double trades — CONFIRMED, Medium
No flock, PID file, lock, or shared-state check exists anywhere in
the package (grep-verified; asserted in the test against the
module source). Proof:
`test_fault_two_runner_instances_trade_the_same_day_twice` — two
runners over one account each execute the full day: 2 entries,
186 shares total for a 93-share signal. systemd prevents a second
*service*, but the realistic trigger is an operator running
`python -m alpaca.runner` by hand to debug while the service is
live — precisely what this project's workflow does on the box.
*Fix:* take an exclusive flock on the state directory in `main()`
and refuse to start if held.

### F8 — Corrupt state file = infinite crash loop — CONFIRMED, Medium
`StateStore.save` is atomic (tmp + `os.replace`, runner.py:454 —
verified SAFE below), but `load()` (runner.py:447) lets
`json.JSONDecodeError` propagate out of `run_day` untouched: no
quarantine, no fallback, no halt-with-exit-3. Under
`Restart=on-failure` the process crash-loops every 30 s forever,
and any position open at the broker is unmanaged meanwhile. Proof:
`test_fault_corrupt_state_file_crash_loops_forever`.
*Fix:* on decode failure, move the bad file to
`runner_state.corrupt-<ts>.json`, write a halted state, exit 3.

### F9 — Out-of-order bars pass through unguarded — CONFIRMED, Low
`AlpacaBarSource.bars_for_day` dedupes by timestamp via a `seen`
set but yields in arrival order. Proof:
`test_fault_bar_source_yields_out_of_order_bars_unguarded` — a
09:30 bar delivered one poll late is emitted *after* 09:31, and
`ORBStrategy` (which keeps only a previous-bar reference and a
running range) consumes it as if sequential. Impact is bounded
(range/entry state built on a permuted tape) and the IEX API is
normally ordered — hence Low — but nothing would even log it.
*Fix:* track the last yielded timestamp; drop (and log) anything
older.

### F12 — Unbounded pagination in `get_bars` — CONFIRMED, Low
client.py:294: `while True:` following `next_page_token` with no
page cap and no token-change check. A token that never clears
hangs the bar fetch indefinitely — and because the flatten hangs
off the bar iterator (F11), it hangs the exit too. Proof:
`test_fault_get_bars_pagination_has_no_page_cap` (the client was
still paging when the test aborted it at 25 calls).
*Fix:* cap at a generous page count and raise beyond it.

### F13 — Redirects walk credentials off the locked host — CONFIRMED, Low
`urllib_transport` (client.py:87) uses a default opener: redirects
are followed automatically, to any host, and the `APCA-*` headers
ride along (empirically proven against loopback servers in
`test_fault_transport_follows_redirects_off_the_locked_host`: a
302 target received the request *and* the key-ID header).
`assert_allowed_url` validates only the URL the client built —
never a redirect target. The project's Databento helper already
refuses redirects outright; this client deviates from that
discipline. Exploitation needs Alpaca (or a MITM on the path) to
issue a redirect, hence Low — but the paper guarantee is supposed
to be structural, and this is a hole in the structure.
*Fix:* a NoRedirect handler that raises on any 3xx.

### F15 — The service runs two copies of the runner module — CONFIRMED, Low (benign today)
`alpaca/__init__.py` eagerly imports `.runner`; the unit runs
`python -m alpaca.runner`, so runpy imports the package (copy 1:
`alpaca.runner`) and then executes the same file again as
`__main__` (copy 2) — the RuntimeWarning in the box journal,
reproduced by `test_entrypoint_m_flag_double_executes_runner_module`.
Every class in runner.py (`Orb15Runner`, `OrderError`,
`ReconciliationHalt`, …) therefore exists twice in the service
process. It is benign *today* because `main()` and everything it
calls live consistently in the `__main__` copy, and nothing outside
runner.py catches its exceptions by class identity (grep-verified:
only tests and the replay script import it, in normal
single-copy processes). It becomes a real bug the day any code
does `isinstance`/`except` across the boundary — the classic
symptom would be a halt exception escaping its handler. 
*Fix:* make `__init__.py` lazy (PEP 562 `__getattr__`) or start
the service with a plain entry script.

---

## Annex — uncapped sizing measured on the real tape (F5/F14)

Frozen ORB-15 rules replayed over `data/spy_1m_polygon.csv`
(501 days with a complete 15-bar opening range, 499 with an
entry; shares = floor($150 / stop distance), uncapped):

| Metric | Value |
|---|---|
| Minimum range width | $0.48 (2024-12-05) |
| Minimum stop distance | $0.50 (2024-12-05) |
| Largest request | **300 shares @ $607.87 = $182,361** (2024-12-05) |
| Next largest | 263 sh / $160,351 (2025-02-19); 263 sh / $159,399 (2024-12-04); 234 sh / $155,098 (2025-09-22); 254 sh / $148,420 (2024-10-21) |
| Days requesting > $100k notional | 48 of 499 (9.6%) |
| Days requesting > $400k (paper buying power) | 0 |
| Median range width | $1.68 (≈ 89 shares, ~$65–115k notional at tape prices) |

For scale: Matt's actual live budget under discussion is a $500
account. Even the *median* ORB-15 SPY trade is a ~$100k-notional
order; that gap is the strategy's design (risk-defined sizing),
and it is exactly why the MES expression of the strategy — not
this share-based one — is the live candidate. This audit takes no
position on that choice; it only establishes that nothing in the
code would stop the share-based runner from asking.

## Checked and found SAFE

* **Paper host lock, including disguises.** Exact-prefix match
  with a path boundary (`config.py:78`); the data host is pinned
  by equality at construction (`config.py` `__post_init__`).
  Scheme downgrade (`http://`), userinfo disguise
  (`https://paper-api.alpaca.markets@evil…`), look-alike
  subdomains on both hosts, and the live host are all refused
  before any transport runs —
  `test_safe_allowed_url_rejects_disguised_hosts` (+ pre-existing
  client tests).
* **Live-key refusal.** A key ID not starting with `PK` cannot
  construct a config (`config.py:130`).
* **Kill-switch parsing.** Arms only on `1/true/yes/on`
  (case-insensitive, whitespace-stripped); absent, typo'd
  (`ture`), or any other value stays OFF —
  `test_safe_runner_enabled_parsing_matrix`.
* **Credential hygiene in parsing.** Both values are stripped in
  `from_env`, so paste whitespace cannot silently corrupt auth
  (the 2026-10-21 401 was a genuinely mismatched pair, hash-proven
  at the time) — `test_safe_credential_whitespace_is_stripped…`.
* **Live-frontier gate.** A restart can never re-trade a historical
  signal (the 2026-10-21 catch-up bug class): gate enforced inside
  `_place_entry_order` (runner.py:667); exits deliberately
  ungated. Pre-existing tests + the restart half of the F5 test.
* **Restart adoption & overnight leftover.** A position matching
  state exactly is adopted and managed; a matching overnight
  leftover is flattened and booked at startup (pre-existing tests
  `test_restart_mid_position_adopts_and_continues`,
  `test_overnight_leftover_flattened_and_booked`). The F1 orphan is
  dangerous precisely because it falls *outside* this covered
  case: state has no record of it.
* **Atomic state writes.** tmp-file + `os.replace`
  (runner.py:454); a failed replace leaves the prior state intact
  — `test_safe_statestore_write_is_atomic`. (Recovery from an
  already-corrupt file is F8.)
* **Partial ENTRY fills.** When an order ends partially filled,
  the broker returns the filled qty and the runner books exactly
  that, with an `entry_partial_fill` event —
  `test_safe_partial_entry_fill_books_only_filled_qty`. (Partial
  *exit* fills are F4; a *stuck* partially-filled status is F2.)
* **Rejected/canceled orders are never assumed filled**
  (pre-existing `test_alpaca_broker_rejection_raises`,
  `test_alpaca_broker_unfilled_raises_not_assumes`).
* **Halts are sticky and loud-at-the-process-level.** A halted
  state stays halted across days, and the unit's
  `SuccessExitStatus=3` keeps systemd from auto-restarting a halt
  (deploy unit, read-only inspection).
* **Time handling.** All runner clocks are
  `ZoneInfo("America/New_York")` instants; the 09:20 ET serve
  window is wall-clock ET and behaves identically across both
  2026 DST transitions; a Saturday start waits for Monday —
  `test_safe_dst_rollover_window_uses_et_wall_clock`.
* **Full holidays.** A zero-bar day completes cleanly as
  `no_opening_range_bars` with zero orders —
  `test_safe_holiday_with_zero_bars_completes_clean`.

## Could not determine (offline audit limits)

* Whether Alpaca's paper API ever issues redirects in practice —
  F13 is proven as a *capability* of the transport against
  loopback servers; no redirect from the real API was observed,
  and this audit used no credentials and touched no live service.
* Alpaca's exact treatment of a market order submitted after an
  early close (reject vs queue-for-next-open). F6's runner-side
  behavior — calendar blindness, order emitted at end-of-data,
  crash if rejected — is proven; the broker-side branch is
  Alpaca's.
* Sub-second race behavior inside Alpaca's matching (e.g. a fill
  landing between the runner's last poll and an order-state
  change) — outside what mocked transports can establish.
* True SIGTERM timing (signal arriving *inside* the HTTP write):
  F1's process-death parametrization uses `SystemExit` raised at
  the same program point, which is the closest offline analogue;
  the state consequences are identical by construction (nothing is
  saved between the broker call and the position assignment).

*Audit deliverables: this report + `tests/test_alpaca_faults.py`.
No production code was modified; the box service was not touched.*
