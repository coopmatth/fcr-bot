"""HOSTILE fault-injection tests for the ORB-15 Alpaca PAPER runner.

Companion to docs/alpaca_runner_audit.md. These tests exist to PROVE
audit findings, not to specify desired behavior:

* Tests named ``test_fault_*`` DOCUMENT UNSAFE BEHAVIOR. They assert
  the unsafe outcome the current code actually produces, so the
  suite stays green while the hazard is on the record. If a fix
  lands, these tests are supposed to start failing — that failure
  is the signal to update the audit and rename the test.
* Tests named ``test_safe_*`` assert behavior that was audited and
  found correct; they are regression guards for the safe verdicts.

Everything is offline: SimulatedBroker / scripted stub clients /
local loopback HTTP servers only. No real credentials, no network
beyond 127.0.0.1.
"""
import json
import subprocess
import sys
import threading
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alpaca.client import AlpacaAPIError, AlpacaClient, urllib_transport
from alpaca.config import (
    AlpacaConfig,
    PaperOnlyError,
    assert_allowed_url,
    runner_enabled,
)
from alpaca.runner import (
    SPY_META,
    AlpacaBarSource,
    AlpacaBroker,
    EventLog,
    Fill,
    Orb15Runner,
    OrderError,
    SimulatedBroker,
    StateStore,
    priced_bars,
)
from fcr.patterns import Bar

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 21)
NEXT_DAY = date(2026, 10, 22)
CLOCK_END = datetime(2026, 10, 21, 16, 5, tzinfo=ET)


def mkbar(day, hh, mm, o, h, l, c):
    return Bar(
        ts=datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET),
        open=o, high=h, low=l, close=c, volume=10000.0,
    )


def range_bars(day=DAY, hi=100.0, lo=99.0, close=99.5):
    """09:30-09:44, every bar spanning the full opening range."""
    return [mkbar(day, 9, 30 + i, close, hi, lo, close) for i in range(15)]


def make_runner(broker, enabled=True, store=None):
    events = []
    store = store or StateStore()
    runner = Orb15Runner(
        broker,
        enabled=enabled,
        log=EventLog(sink=events),
        store=store,
        clock=lambda: CLOCK_END,
    )
    return runner, events, store


def run(runner, broker, bars, day=DAY):
    return runner.run_day(day, priced_bars(bars, broker))


def event_types(events):
    return [e["type"] for e in events]


def long_entry_bars(day=DAY):
    """Range 100/99; 09:45 close 100.6 -> long 93 sh, stop 99,
    target 103.8. No exit bar follows."""
    return range_bars(day=day) + [
        mkbar(day, 9, 45, 99.8, 100.7, 99.7, 100.6),
    ]


# ======================================================================
# 1. Order lifecycle
# ======================================================================
class GhostFillBroker:
    """The adversary: place_market APPLIES the fill on the account
    and only then fails (HTTP timeout after Alpaca received the
    POST, or the process dying between fill and state save). The
    runner never learns the order existed."""

    def __init__(self, exc):
        self._exc = exc
        self._qty = 0
        self.exit_attempts = 0

    def set_price(self, price):  # priced_bars wiring; price is moot
        pass

    def position_qty(self, symbol):
        return self._qty

    def place_market(self, symbol, qty, side):
        if self._qty == 0:
            self._qty += qty if side == "buy" else -qty
            raise self._exc
        self.exit_attempts += 1
        raise AssertionError("no exit should ever be attempted here")


@pytest.mark.parametrize(
    "exc",
    [
        AlpacaAPIError("Network error calling Alpaca: timed out"),
        SystemExit(1),  # SIGTERM analogue: death between fill and save
    ],
    ids=["http_timeout_after_fill", "process_death_after_fill"],
)
def test_fault_fill_then_failure_orphans_position_and_halts_forever(exc):
    """UNSAFE (audit F1): an entry fill the runner never sees leaves
    a REAL position the state file knows nothing about. Restart
    reconciliation (correctly) refuses to guess and halts — but the
    halt never flattens the orphan, and the halted state persists,
    so the position rides unmanaged until a human intervenes."""
    broker = GhostFillBroker(exc)
    runner, events, store = make_runner(broker)
    with pytest.raises(type(exc)):
        run(runner, broker, long_entry_bars())
    # The account holds 93 SPY; the persisted state says flat.
    assert broker.position_qty("SPY") == 93
    assert store.load()["position"] is None

    # systemd restarts the process: reconciliation halts the day.
    runner2, events2, _ = make_runner(broker, store=store)
    summary2 = run(runner2, broker, long_entry_bars())
    assert summary2["halted"] is True
    assert "unknown holding" in summary2["halt_reason"]
    # The halt does NOT flatten the orphan position...
    assert broker.position_qty("SPY") == 93
    assert broker.exit_attempts == 0

    # ...and the halt is sticky: the next day halts too, position
    # still open. Only a human clearing the state file ends this.
    runner3, _, _ = make_runner(broker, store=store)
    summary3 = run(runner3, broker, long_entry_bars(NEXT_DAY), day=NEXT_DAY)
    assert summary3["halted"] is True
    assert broker.position_qty("SPY") == 93


class ScriptedOrderClient:
    """Duck-typed AlpacaClient stand-in for AlpacaBroker tests."""

    def __init__(self, order_script, position=None):
        self._script = list(order_script)
        self._position = position
        self.placed = []
        self.polls = 0
        self.cancels = []

    def place_market_order(self, symbol, qty, side):
        self.placed.append((symbol, qty, side))
        return {"id": "ord-1", "status": "accepted"}

    def get_order(self, order_id):
        self.polls += 1
        return self._script.pop(0) if self._script else self._script

    def get_position(self, symbol):
        return self._position

    def cancel_order(self, order_id):  # pragma: no cover - never called
        self.cancels.append(order_id)


def test_fault_partially_filled_status_polls_out_and_order_is_never_canceled():
    """UNSAFE (audit F2): an order stuck in 'partially_filled' is
    neither 'filled' nor a final-bad status, so AlpacaBroker polls
    it to max_polls and raises OrderError — while shares have in
    fact filled on the account. The codebase has NO cancel-order
    capability at all, so the live order is left to fill further,
    unmanaged."""
    stuck = {
        "id": "ord-1", "status": "partially_filled",
        "filled_qty": "40", "filled_avg_price": "100.60",
    }
    client = ScriptedOrderClient([stuck] * 10, position={"qty": "40"})
    broker = AlpacaBroker(client, sleep_fn=lambda s: None, max_polls=5)
    with pytest.raises(OrderError):
        broker.place_market("SPY", 93, "buy")
    assert client.polls == 5
    assert client.placed == [("SPY", 93, "buy")]  # exactly one POST
    assert client.cancels == []                    # never canceled
    # Structural fact: the client class offers no cancellation path.
    assert not hasattr(AlpacaClient, "cancel_order")


class ExitRejectingBroker(SimulatedBroker):
    """Entries fill; every exit attempt fails (rejected order)."""

    def __init__(self):
        super().__init__()
        self.exit_attempts = 0

    def place_market(self, symbol, qty, side):
        if self._qty != 0:
            self.exit_attempts += 1
            raise OrderError("Alpaca order rejected (simulated)")
        return super().place_market(symbol, qty, side)


def test_fault_exit_rejection_at_flatten_crashes_and_retries_only_via_restart():
    """UNSAFE (audit F3): the end-of-day flatten's order failing
    propagates as an uncaught exception out of run_day — there is
    no in-run retry and no alerting, only a process crash. The one
    retry that ever happens comes from systemd restarting the
    process and replaying the whole day."""
    broker = ExitRejectingBroker()
    runner, events, store = make_runner(broker)
    bars = long_entry_bars() + [
        mkbar(DAY, 15, 30, 101.0, 101.2, 100.8, 101.0),
    ]
    with pytest.raises(OrderError):
        run(runner, broker, bars)
    # Position still open on the account AND in persisted state.
    assert broker.position_qty("SPY") == 93
    assert store.load()["position"] is not None
    assert broker.exit_attempts == 1  # exactly one attempt, no retry

    # Process restart (systemd Restart=on-failure): the day replays,
    # the position is adopted, the flatten is attempted a second
    # time — and fails the same way. Nothing escalates; the only
    # trace is lines in a log file.
    runner2, events2, _ = make_runner(broker, store=store)
    with pytest.raises(OrderError):
        run(runner2, broker, bars)
    assert broker.exit_attempts == 2
    assert broker.position_qty("SPY") == 93
    assert not any("alert" in t for t in event_types(events + events2))


def test_safe_partial_entry_fill_books_only_filled_qty():
    """SAFE: when an order reaches a final state partially filled,
    AlpacaBroker returns the filled quantity and the runner books
    exactly that position (and logs entry_partial_fill)."""
    client = ScriptedOrderClient([
        {"id": "ord-1", "status": "canceled", "filled_qty": "40",
         "filled_avg_price": "100.60"},
        {"id": "ord-2", "status": "filled", "filled_qty": "40",
         "filled_avg_price": "103.90"},
    ])
    broker = AlpacaBroker(client, sleep_fn=lambda s: None)
    runner, events, _ = make_runner(broker)
    bars = range_bars() + [
        mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6),
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),  # target bar
    ]
    # AlpacaBroker doesn't price off bars; drive run_day directly.
    summary = runner.run_day(DAY, bars)
    assert summary["trades"] == 1
    assert "entry_partial_fill" in event_types(events)
    assert client.placed == [("SPY", 93, "buy"), ("SPY", 40, "sell")]
    assert summary["realized_pnl_usd"] == round(40 * (103.9 - 100.6), 2)


class PartialExitBroker(SimulatedBroker):
    """Entry fills fully; the FIRST exit fills only 40 shares."""

    def __init__(self):
        super().__init__()
        self._exit_calls = 0

    def place_market(self, symbol, qty, side):
        if self._qty != 0:
            self._exit_calls += 1
            if self._exit_calls == 1:
                fill = Fill(price=self._price, qty=40, order_id="part-1")
                self._qty -= 40
                self.orders.append(
                    {"id": "part-1", "symbol": symbol, "qty": 40,
                     "side": side, "price": self._price}
                )
                return fill
        return super().place_market(symbol, qty, side)


def test_fault_partial_exit_fill_leaves_remainder_unmanaged_until_eod():
    """UNSAFE (audit F4): after a partial exit fill the runner marks
    the trade DONE with the strategy, so the remaining 53 shares
    get NO stop/target management for the rest of the day — even on
    a bar that smashes through the original stop. They exit only
    via the end-of-data flatten."""
    broker = PartialExitBroker()
    runner, events, _ = make_runner(broker)
    bars = range_bars() + [
        mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6),   # entry 93
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),  # target: exit 40
        mkbar(DAY, 10, 0, 100.0, 100.5, 90.0, 95.0),   # stop smashed
        mkbar(DAY, 15, 30, 95.0, 95.5, 94.5, 95.0),
    ]
    summary = run(runner, broker, bars)
    # Two exit orders total: the partial 40 at the target bar, then
    # 53 at end of data. NOTHING was placed on the 10:00 stop bar.
    exits = [o for o in broker.orders if o["side"] == "sell"]
    assert [(o["qty"]) for o in exits] == [40, 53]
    assert "flatten_enforced" in event_types(events)
    assert summary["trades"] == 2  # partial + flatten booked as two


# ======================================================================
# 2. Sizing
# ======================================================================
def test_fault_sizing_is_uncapped_and_a_rejection_crashes_the_day():
    """UNSAFE (audit F5): SPY sizing has no cap (SPY_META.max_units
    is None). On real Polygon tape the worst day (2024-12-05,
    $0.50 stop distance) requested 300 shares / ~$182k notional —
    364x the $500 overlay (docs/alpaca_runner_audit.md). Here a
    300-share request is proven executable, and a buying-power
    rejection on entry propagates as an uncaught crash."""
    assert SPY_META.max_units is None
    # Geometry for exactly floor(150 / 0.50) = 300 shares.
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    bars = range_bars(hi=100.49, lo=100.00, close=100.25) + [
        mkbar(DAY, 9, 45, 100.3, 100.55, 100.2, 100.50),
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),
    ]
    run(runner, broker, bars)
    signal = [e for e in events if e["type"] == "signal"]
    assert signal[0]["shares"] == 300
    assert broker.orders[0]["qty"] == 300  # nothing capped it

    class BuyingPowerBroker(SimulatedBroker):
        def place_market(self, symbol, qty, side):
            raise AlpacaAPIError(
                "insufficient buying power", status=403
            )

    broker2 = BuyingPowerBroker()
    runner2, _, store2 = make_runner(broker2)
    with pytest.raises(AlpacaAPIError):
        run(runner2, broker2, bars)
    assert store2.load()["position"] is None

    # After the crash, a restart (systemd) converts the day to a
    # missed_day via the live-frontier gate — no second attempt.
    runner3, events3, _ = make_runner(broker2, store=store2)
    summary3 = runner3.run_day(
        DAY, priced_bars(bars, broker2),
        live_since=datetime(2026, 10, 21, 14, 0, tzinfo=ET),
    )
    assert summary3["missed_day"] is True
    assert "missed_day" in event_types(events3)


# ======================================================================
# 3. Market calendar
# ======================================================================
def test_early_close_day_flattens_at_end_of_data_not_at_1555():
    """UNSAFE-by-omission (audit F6): the runner has NO market
    calendar awareness (AlpacaClient exposes no calendar/clock
    method and nothing calls one). On a 13:00 early close the
    strategy's 15:55 flatten bar never exists; the position is
    saved only by the end-of-data flatten fallback — whose order
    goes out whenever the feed ends, into a closed market."""
    assert not hasattr(AlpacaClient, "get_calendar")
    assert not hasattr(AlpacaClient, "get_clock")
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    bars = long_entry_bars() + [
        mkbar(DAY, 12, 59, 101.0, 101.2, 100.8, 101.0),
        mkbar(DAY, 13, 0, 101.0, 101.1, 100.9, 101.0),  # last bar
    ]
    summary = run(runner, broker, bars)
    assert summary["trades"] == 1
    assert "flatten_enforced" in event_types(events)
    assert broker.position_qty("SPY") == 0


def test_safe_holiday_with_zero_bars_completes_clean():
    """SAFE: a full holiday (no bars at all) completes as a
    no-opening-range day with zero orders and clean state."""
    broker = SimulatedBroker()
    runner, _, store = make_runner(broker)
    summary = runner.run_day(DAY, [])
    assert summary["trades"] == 0
    assert summary["no_trade_reason"] == "no_opening_range_bars"
    assert broker.orders == []
    assert store.load()["position"] is None


# ======================================================================
# 4. Concurrency & process
# ======================================================================
def test_fault_two_runner_instances_trade_the_same_day_twice():
    """UNSAFE (audit F7): there is no single-instance guard
    anywhere — no file lock, no PID file, no shared-state check.
    Two runners over the same account (e.g. the systemd service
    plus a manual `python -m alpaca.runner` while debugging) each
    run the full day: the same signal is traded TWICE (2x the
    intended $150 risk)."""
    import alpaca.runner as runner_mod

    source = Path(runner_mod.__file__).read_text()
    assert "flock" not in source and "O_EXCL" not in source

    broker = SimulatedBroker()
    bars = range_bars() + [
        mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6),
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),
    ]
    runner_a, _, _ = make_runner(broker)
    run(runner_a, broker, bars)
    runner_b, _, _ = make_runner(broker)  # second instance, own state
    run(runner_b, broker, bars)
    entries = [o for o in broker.orders if o["side"] == "buy"]
    assert len(entries) == 2
    assert sum(o["qty"] for o in entries) == 186  # 93 + 93: doubled


def test_safe_statestore_write_is_atomic(tmp_path):
    """SAFE: StateStore.save writes tmp + os.replace — if the
    replace fails, the previous state file survives intact."""
    path = tmp_path / "runner_state.json"
    store = StateStore(path)
    original = {"date": "2026-10-21", "position": None, "v": 1}
    store.save(original)
    import alpaca.runner as runner_mod

    real_replace = runner_mod.os.replace

    def boom(src, dst):
        raise OSError("simulated crash mid-save")

    runner_mod.os.replace = boom
    try:
        with pytest.raises(OSError):
            store.save({"date": "2026-10-21", "position": None, "v": 2})
    finally:
        runner_mod.os.replace = real_replace
    assert store.load() == original


def test_fault_corrupt_state_file_crash_loops_forever(tmp_path):
    """UNSAFE (audit F8): a corrupt state file makes load() raise
    JSONDecodeError straight out of run_day — uncaught, with no
    quarantine or recovery path. Under systemd Restart=on-failure
    this is an infinite 30s crash loop; a position open at the
    broker during the corruption is unmanaged throughout."""
    path = tmp_path / "runner_state.json"
    path.write_bytes(b'{"date": "2026-10-21", "position": {"dire')
    store = StateStore(path)
    broker = SimulatedBroker()
    runner, _, _ = make_runner(broker, store=store)
    with pytest.raises(json.JSONDecodeError):
        runner.run_day(DAY, [])
    # The file is untouched: nothing self-heals.
    assert path.read_bytes().startswith(b'{"date"')


# ======================================================================
# 5. Feed integrity
# ======================================================================
def test_fault_bar_source_yields_out_of_order_bars_unguarded():
    """UNSAFE (audit F9): AlpacaBarSource dedupes by timestamp but
    never enforces monotonic order. A bar that arrives late (the
    data API delivering 09:30 only on the second poll) is yielded
    AFTER 09:31 — and the strategy consumes bars in arrival order,
    so its range/entry state can be built out of order. Nothing
    logs or rejects this."""
    b930 = Bar(ts=datetime(2026, 10, 21, 9, 30, tzinfo=ET),
               open=100, high=101, low=99, close=100.4, volume=1)
    b931 = Bar(ts=datetime(2026, 10, 21, 9, 31, tzinfo=ET),
               open=100.4, high=101, low=100, close=100.6, volume=1)
    b932 = Bar(ts=datetime(2026, 10, 21, 9, 32, tzinfo=ET),
               open=100.6, high=101, low=100, close=100.5, volume=1)
    pages = [[b931], [b930, b931, b932], [b930, b931, b932]]

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def get_bars(self, symbol, start, end, feed="iex"):
            page = pages[min(self.calls, len(pages) - 1)]
            self.calls += 1
            return page

    times = iter([
        datetime(2026, 10, 21, 9, 32, tzinfo=ET),
        datetime(2026, 10, 21, 9, 33, tzinfo=ET),
        datetime(2026, 10, 21, 16, 1, tzinfo=ET),
    ])
    source = AlpacaBarSource(
        FakeClient(), clock=lambda: next(times), sleep_fn=lambda s: None,
    )
    bars = list(source.bars_for_day(DAY))
    assert [b.ts.minute for b in bars] == [31, 30, 32]  # out of order


def test_fault_feed_gap_in_range_window_silently_distorts_range():
    """UNSAFE (audit F10): minutes missing inside 09:30-09:45 are
    invisible to the runner — no gap detection, no event. Here the
    omitted 09:37 bar held the day's true high (105): with it, no
    breakout ever fires; without it, the runner enters a trade the
    frozen strategy — shown the real tape — would never take."""
    full = []
    for i in range(15):
        hi = 105.0 if i == 7 else 100.0  # 09:37 spikes to the true high
        full.append(mkbar(DAY, 9, 30 + i, 99.5, hi, 99.0, 99.5))
    trigger = mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6)
    gapped = [b for b in full if b.ts.minute not in (37, 38)]

    broker_full = SimulatedBroker()
    runner_full, _, _ = make_runner(broker_full)
    run(runner_full, broker_full, full + [trigger])
    assert broker_full.orders == []  # true tape: no trade

    broker_gap = SimulatedBroker()
    runner_gap, events_gap, _ = make_runner(broker_gap)
    run(runner_gap, broker_gap, gapped + [trigger])
    assert len(broker_gap.orders) >= 1  # gapped tape: trade taken
    assert not any("gap" in t for t in event_types(events_gap))


def test_fault_feed_exception_mid_position_means_no_flatten_that_run():
    """UNSAFE (audit F11): exits — including the 15:55 flatten —
    are decided per arriving bar, and the end-of-data flatten runs
    only when the bar iterator ENDS NORMALLY. If the feed raises
    mid-position (data API outage), the exception aborts run_day:
    no flatten, no exit attempt; the position is simply left."""
    broker = SimulatedBroker()
    runner, events, store = make_runner(broker)

    def dying_feed():
        for bar in long_entry_bars():
            broker.set_price(bar.close)
            yield bar
        raise AlpacaAPIError("data API unavailable", status=500)

    with pytest.raises(AlpacaAPIError):
        runner.run_day(DAY, dying_feed())
    assert broker.position_qty("SPY") == 93
    assert store.load()["position"] is not None
    types = event_types(events)
    assert "flatten_enforced" not in types
    assert "trade_closed" not in types


def test_fault_get_bars_pagination_has_no_page_cap():
    """UNSAFE (audit F12): AlpacaClient.get_bars follows
    next_page_token in an unbounded while-loop. A token that never
    clears (API bug / proxy loop) hangs the bar fetch forever —
    and since the flatten depends on the bar iterator, a hung
    fetch also means no flatten."""
    config = AlpacaConfig(key_id="PKTEST", secret_key="sekret")
    calls = {"n": 0}
    payload = {
        "bars": [{"t": "2026-10-21T13:30:00Z", "o": 1, "h": 1,
                  "l": 1, "c": 1, "v": 1}],
        "next_page_token": "loop",
    }

    def transport(method, url, headers, body):
        calls["n"] += 1
        if calls["n"] >= 25:
            raise RuntimeError("test aborts the infinite loop")
        return 200, {}, json.dumps(payload).encode()

    client = AlpacaClient(config, transport=transport)
    with pytest.raises(RuntimeError):
        client.get_bars(
            "SPY",
            datetime(2026, 10, 21, 13, 30, tzinfo=ET),
            datetime(2026, 10, 21, 16, 0, tzinfo=ET),
        )
    assert calls["n"] == 25  # it would have kept going forever


# ======================================================================
# 6. Time / DST
# ======================================================================
class MutableClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _serve_one_probe(start_now):
    clock = MutableClock(start_now)
    called = []

    def factory(day):
        called.append(day)
        return []

    def advance(_seconds):
        clock.now = clock.now + timedelta(days=1)

    runner = Orb15Runner(
        SimulatedBroker(), enabled=False,
        log=EventLog(sink=[]), store=StateStore(), clock=clock,
    )
    runner.serve(factory, sleep_fn=advance, max_days=1)
    return called


def test_safe_dst_rollover_window_uses_et_wall_clock():
    """SAFE: serve()'s 09:20 ET trigger is computed from
    ZoneInfo('America/New_York') wall time, so the Mondays after
    both 2026 clock changes still trigger exactly one day run,
    and a Saturday start waits for Monday."""
    fri_edt = datetime(2026, 10, 30, 9, 25, tzinfo=ET)
    mon_est = datetime(2026, 11, 2, 9, 25, tzinfo=ET)
    mon_edt = datetime(2026, 3, 9, 9, 25, tzinfo=ET)
    assert fri_edt.utcoffset() == timedelta(hours=-4)
    assert mon_est.utcoffset() == timedelta(hours=-5)
    assert mon_edt.utcoffset() == timedelta(hours=-4)
    assert _serve_one_probe(fri_edt) == [date(2026, 10, 30)]
    assert _serve_one_probe(mon_est) == [date(2026, 11, 2)]
    assert _serve_one_probe(mon_edt) == [date(2026, 3, 9)]
    sat = datetime(2026, 10, 31, 10, 0, tzinfo=ET)
    assert _serve_one_probe(sat) == [date(2026, 11, 2)]


# ======================================================================
# 7. Config / credentials / transport
# ======================================================================
def test_safe_runner_enabled_parsing_matrix():
    """SAFE: the kill switch arms ONLY on an explicit truthy set
    ('1'/'true'/'yes'/'on', any case, whitespace tolerated). A
    typo'd value ('ture'), 'enabled', '2', 'off' — all stay OFF."""
    for raw in ["1", "true", "TRUE", "True", "yes", "on", " true "]:
        assert runner_enabled({"ALPACA_RUNNER_ENABLED": raw}) is True, raw
    for raw in ["0", "false", "no", "off", "", "ture", "enabled", "2"]:
        assert runner_enabled({"ALPACA_RUNNER_ENABLED": raw}) is False, raw
    assert runner_enabled({}) is False


def test_safe_credential_whitespace_is_stripped_not_silently_failed():
    """SAFE: from_env strips both credential values, so the stray
    whitespace a copy/paste adds cannot silently corrupt auth
    (the 2026-10-21 401 was a mismatched PAIR, proven by hashes —
    not whitespace)."""
    cfg = AlpacaConfig.from_env({
        "ALPACA_PAPER_KEY_ID": "  PKABC123 \n",
        "ALPACA_PAPER_SECRET": "\tsekret \n",
    })
    assert cfg.key_id == "PKABC123"
    assert cfg.secret_key == "sekret"


def test_safe_allowed_url_rejects_disguised_hosts():
    """SAFE: the URL lock rejects scheme downgrade, userinfo
    disguise and look-alike hosts on BOTH the trading and the
    data host; the two real hosts pass."""
    for bad in [
        "http://paper-api.alpaca.markets/v2/account",
        "https://paper-api.alpaca.markets@evil.example/v2/account",
        "https://paper-api.alpaca.markets.evil.example/v2/account",
        "https://data.alpaca.markets.evil.example/v2/stocks/SPY/bars",
        "https://evil.example/v2/account",
    ]:
        with pytest.raises(PaperOnlyError):
            assert_allowed_url(bad)
    assert assert_allowed_url(
        "https://paper-api.alpaca.markets/v2/account"
    ).startswith("https://paper-api")
    assert assert_allowed_url(
        "https://data.alpaca.markets/v2/stocks/SPY/bars"
    ).startswith("https://data")


def test_fault_transport_follows_redirects_off_the_locked_host():
    """UNSAFE (audit F13): urllib_transport does NOT disable
    redirects (unlike the project's Databento helper, which refuses
    them). A 302 from the 'locked' host is followed to ANY host,
    and the APCA credential headers ride along — the per-request
    assert_allowed_url check covers only the original URL."""
    hits = {}

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            hits["path"] = self.path
            hits["key_id"] = self.headers.get("APCA-API-KEY-ID")
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target_port = target.server_address[1]

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target_port}/stolen"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    redir_port = redirector.server_address[1]
    threads = [
        threading.Thread(target=target.serve_forever, daemon=True),
        threading.Thread(target=redirector.serve_forever, daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        status, _headers, _body = urllib_transport(
            "GET",
            f"http://127.0.0.1:{redir_port}/v2/account",
            {"APCA-API-KEY-ID": "PKTEST",
             "APCA-API-SECRET-KEY": "sekret"},
            None,
        )
    finally:
        target.shutdown()
        redirector.shutdown()
    assert status == 200
    # The request LANDED on the redirect target — a host the lock
    # never approved — carrying the credential header with it.
    assert hits.get("path") == "/stolen"
    assert hits.get("key_id") == "PKTEST"


# ======================================================================
# 8. Slippage
# ======================================================================
class SlippageBroker(SimulatedBroker):
    """Entries fill 1% through the signal price (fast-break chase);
    exits fill at market like the base class."""

    def place_market(self, symbol, qty, side):
        if self._qty == 0:
            slipped = round(self._price * 1.01, 2)
            real_price, self._price = self._price, slipped
            try:
                return super().place_market(symbol, qty, side)
            finally:
                self._price = real_price
        return super().place_market(symbol, qty, side)


def test_fault_slippage_fill_books_without_any_sanity_check():
    """UNSAFE (audit F14): there is NO fill-sanity check anywhere.
    A fill 1% through the signal is booked silently: the stop stays
    anchored to the strategy's levels, so the trade's REAL risk is
    93 x (101.61 - 99.00) = $242.73 — 1.62x the $150 budget — and
    no event records the divergence."""
    broker = SlippageBroker()
    runner, events, _ = make_runner(broker)
    bars = range_bars() + [
        mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6),   # signal close
        mkbar(DAY, 9, 46, 100.6, 101.0, 100.2, 100.8),
        mkbar(DAY, 9, 47, 100.8, 100.9, 98.9, 99.2),   # stop bar
    ]
    summary = run(runner, broker, bars)
    filled = [e for e in events if e["type"] == "order_filled"]
    assert filled[0]["fill_price"] == pytest.approx(101.61)
    # Stop unchanged at the strategy level; realized loss is booked
    # honestly off the slipped fill — the RISK was the lie, not the
    # bookkeeping: the position risked $242.73 against a $150 spec.
    assert summary["realized_pnl_usd"] == round(93 * (99.2 - 101.61), 2)
    types = event_types(events)
    assert not any(
        ("slip" in t or "divergence" in t or "warning" in t) for t in types
    )


# ======================================================================
# 9. The -m entrypoint module duplication
# ======================================================================
def test_entrypoint_m_flag_double_executes_runner_module():
    """AUDIT (audit F15): the deployed unit runs
    `python -m alpaca.runner`, and alpaca/__init__ eagerly imports
    .runner — so runpy executes runner.py TWICE (once as
    alpaca.runner, once as __main__), emitting the RuntimeWarning
    seen in the box journal. Two copies of the module's classes
    exist in that process. Verified benign TODAY (nothing crosses
    the copy boundary: main() uses its own copy consistently and
    no other module catches runner exceptions by class), but it is
    a latent identity hazard for any future isinstance/except use.
    This test pins the current behavior so it cannot change
    silently."""
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-W", "always", "-m", "alpaca.runner", "--help"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "RuntimeWarning" in proc.stderr
    assert "alpaca.runner" in proc.stderr
