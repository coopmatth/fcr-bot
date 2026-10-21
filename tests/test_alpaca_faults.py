"""HOSTILE fault-injection tests for the ORB-15 Alpaca PAPER runner.

Companion to docs/alpaca_runner_audit.md. These tests exist to PROVE
audit findings, not to specify desired behavior:

* Tests named ``test_fault_*`` DOCUMENT UNSAFE BEHAVIOR. They assert
  the unsafe outcome the current code actually produces, so the
  suite stays green while the hazard is on the record. If a fix
  lands, these tests are supposed to start failing — that failure
  is the signal to update the audit and rename the test.
* Tests named ``test_fixed_*`` are flipped ``test_fault_*`` tests:
  the finding was fixed in the 2026-10-21 hardening pass and the
  test now asserts the SAFE behavior under the same adversarial
  conditions. Names keep the finding ID (F1, F2, ...) traceable.
  Findings F9/F10/F12/F14/F15 were OUT of the hardening scope and
  their ``test_fault_*`` tests remain as-is — the unsafe behavior
  they document is still the behavior.
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
    InstanceLock,
    Orb15Runner,
    OrderError,
    RunnerPosition,
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


def make_runner(broker, enabled=True, store=None, **runner_kwargs):
    events = []
    store = store or StateStore()
    runner = Orb15Runner(
        broker,
        enabled=enabled,
        log=EventLog(sink=events),
        store=store,
        clock=lambda: CLOCK_END,
        **runner_kwargs,
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
    runner never learns the order existed. close_position works
    normally — that is the F1 orphan policy's tool."""

    def __init__(self, exc):
        self._exc = exc
        self._qty = 0
        self.exit_attempts = 0
        self.close_calls = 0

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

    def close_position(self, symbol):
        self.close_calls += 1
        qty = abs(self._qty)
        self._qty = 0
        return Fill(price=100.6, qty=qty, order_id="ghost-close-1")


@pytest.mark.parametrize(
    "exc",
    [
        AlpacaAPIError("Network error calling Alpaca: timed out"),
        SystemExit(1),  # SIGTERM analogue: death between fill and save
    ],
    ids=["http_timeout_after_fill", "process_death_after_fill"],
)
def test_fixed_F1_orphan_position_is_flattened_and_day_halts(exc):
    """FIXED (audit F1, hardening 2026-10-21): an entry fill the
    runner never sees still orphans the position for the rest of
    THAT run — but at restart, reconciliation's ORPHAN POLICY
    flattens it via close_position, logs orphan_flattened with the
    full detail, and halts entries for the day. The position never
    rides unmanaged into another day."""
    broker = GhostFillBroker(exc)
    runner, events, store = make_runner(broker)
    with pytest.raises(type(exc)):
        run(runner, broker, long_entry_bars())
    # The account holds 93 SPY; the persisted state says flat.
    assert broker.position_qty("SPY") == 93
    assert store.load()["position"] is None

    # systemd restarts the process: reconciliation flattens the
    # orphan, then halts the day (no entries on a day whose books
    # were wrong at the open).
    runner2, events2, _ = make_runner(broker, store=store)
    summary2 = run(runner2, broker, long_entry_bars())
    assert summary2["halted"] is True
    assert "orphan policy" in summary2["halt_reason"]
    types2 = event_types(events2)
    assert "orphan_detected" in types2
    assert "orphan_flattened" in types2
    flattened = [e for e in events2 if e["type"] == "orphan_flattened"]
    assert flattened[0]["flattened_qty"] == 93
    assert flattened[0]["pnl_booked_usd"] == 0.0  # entry fill unknown
    # The orphan is FLAT — not riding, not compounded.
    assert broker.position_qty("SPY") == 0
    assert broker.close_calls == 1

    # The halt stays sticky (a human reviews the day), but there
    # is no position left to mismanage.
    runner3, _, _ = make_runner(broker, store=store)
    summary3 = run(runner3, broker, long_entry_bars(NEXT_DAY), day=NEXT_DAY)
    assert summary3["halted"] is True
    assert broker.position_qty("SPY") == 0
    assert broker.close_calls == 1  # no repeat flatten


class LostResponseClient:
    """Duck-typed client for the broker-level F1 recovery: the
    POST raises a transport error AFTER Alpaca accepted the
    order; the order is findable ONLY by its client_order_id."""

    def __init__(self, refusal=None):
        self.posts = 0
        self.lookups = 0
        self.seen_cid = None
        self._refusal = refusal
        self._order = {
            "id": "ord-lost", "status": "filled",
            "filled_qty": "93", "filled_avg_price": "100.60",
        }

    def place_market_order(self, symbol, qty, side, client_order_id=None):
        self.posts += 1
        self.seen_cid = client_order_id
        if self._refusal is not None:
            raise self._refusal
        raise AlpacaAPIError("Network error calling Alpaca: timed out")

    def get_order_by_client_order_id(self, client_order_id):
        self.lookups += 1
        assert client_order_id == self.seen_cid
        return dict(self._order)

    def get_order(self, order_id):
        return dict(self._order)

    def get_position(self, symbol):
        return {"qty": "93"}


def test_fixed_F1_unknown_submission_outcome_recovers_by_client_order_id():
    """FIXED (audit F1): a submission whose response was LOST is
    recovered by looking the order up under its deterministic
    client_order_id — exactly ONE POST ever reaches Alpaca (no
    duplicate order, no orphan), and the fill is booked."""
    client = LostResponseClient()
    broker = AlpacaBroker(client, sleep_fn=lambda s: None)
    broker.set_client_order_id("orb15-2026-10-21-entry")
    fill = broker.place_market("SPY", 93, "buy")
    assert fill.qty == 93
    assert fill.price == pytest.approx(100.60)
    assert client.posts == 1     # idempotent: never re-POSTed
    assert client.lookups == 1   # recovered by lookup instead


def test_fixed_F1_definitive_refusal_is_not_looked_up():
    """FIXED (audit F1): when Alpaca ANSWERS the POST with a
    refusal (HTTP 403), the outcome is KNOWN — nothing was
    created — so the broker raises OrderError immediately without
    a pointless lookup."""
    client = LostResponseClient(
        refusal=AlpacaAPIError("insufficient buying power", status=403)
    )
    broker = AlpacaBroker(client, sleep_fn=lambda s: None)
    broker.set_client_order_id("orb15-2026-10-21-entry")
    with pytest.raises(OrderError):
        broker.place_market("SPY", 93, "buy")
    assert client.posts == 1
    assert client.lookups == 0


class ScriptedOrderClient:
    """Duck-typed AlpacaClient stand-in for AlpacaBroker tests."""

    def __init__(self, order_script, position=None):
        self._script = list(order_script)
        self._position = position
        self.placed = []
        self.polls = 0
        self.cancels = []

    def place_market_order(self, symbol, qty, side, client_order_id=None):
        self.placed.append((symbol, qty, side))
        self.placed_cids = getattr(self, "placed_cids", [])
        self.placed_cids.append(client_order_id)
        return {"id": "ord-1", "status": "accepted"}

    def get_order(self, order_id):
        self.polls += 1
        return self._script.pop(0) if self._script else self._script

    def get_position(self, symbol):
        return self._position

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        return {"id": order_id, "status": "canceled"}


def test_fixed_F2_partially_filled_cancels_remainder_and_books_filled():
    """FIXED (audit F2, hardening 2026-10-21): 'partially_filled'
    is a LIVE state — the broker keeps polling to its bounded
    deadline, then CANCELS the unfilled remainder and books
    exactly the filled quantity. The client now HAS a cancel path.
    Nothing is left live and unmanaged."""
    stuck = {
        "id": "ord-1", "status": "partially_filled",
        "filled_qty": "40", "filled_avg_price": "100.60",
    }
    client = ScriptedOrderClient([stuck] * 10, position={"qty": "40"})
    broker = AlpacaBroker(client, sleep_fn=lambda s: None, max_polls=5)
    fill = broker.place_market("SPY", 93, "buy")
    assert fill.qty == 40
    assert fill.price == pytest.approx(100.60)
    assert client.polls == 5
    assert client.placed == [("SPY", 93, "buy")]  # exactly one POST
    assert client.cancels == ["ord-1"]            # remainder canceled
    assert hasattr(AlpacaClient, "cancel_order")


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


def test_fixed_F3_exit_rejection_falls_back_to_close_position():
    """FIXED (audit F3, hardening 2026-10-21): an exit order's
    rejection no longer kills the run. The exit path falls back
    to close_position ONCE and the flatten completes; the failure
    and the fallback are on the event record."""
    broker = ExitRejectingBroker()
    runner, events, store = make_runner(broker)
    bars = long_entry_bars() + [
        mkbar(DAY, 15, 30, 101.0, 101.2, 100.8, 101.0),
    ]
    summary = run(runner, broker, bars)  # no exception escapes
    assert summary["trades"] == 1
    assert summary["exit_failures"] == 0
    assert broker.exit_attempts == 1  # the market order was tried...
    closes = [o for o in broker.orders if o.get("kind") == "close_position"]
    assert len(closes) == 1           # ...then close_position worked
    assert broker.position_qty("SPY") == 0
    assert store.load()["position"] is None
    types = event_types(events)
    assert "exit_order_failed" in types
    assert "exit_failure_critical" not in types
    assert "flatten_enforced" in types


class TotalExitFailureBroker(SimulatedBroker):
    """Entries fill; EVERY exit mechanism fails (market orders
    rejected AND close_position erroring)."""

    def __init__(self):
        super().__init__()
        self.exit_attempts = 0
        self.close_attempts = 0

    def place_market(self, symbol, qty, side):
        if self._qty != 0:
            self.exit_attempts += 1
            raise OrderError("Alpaca order rejected (simulated)")
        return super().place_market(symbol, qty, side)

    def close_position(self, symbol):
        self.close_attempts += 1
        raise OrderError("close_position failed (simulated)")


def test_fixed_F3_total_exit_failure_is_critical_logged_and_run_survives():
    """FIXED (audit F3, contained version): when the market order
    AND the close_position fallback both fail, the run does NOT
    crash — it logs exit_failure_critical, keeps the position
    under management, RETRIES the exit on subsequent bars, and
    completes the day with the position still in the persisted
    state (tomorrow's reconciliation owns it from there)."""
    broker = TotalExitFailureBroker()
    runner, events, store = make_runner(broker)
    bars = range_bars() + [
        mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6),    # entry 93
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),  # target: exit fails
        mkbar(DAY, 9, 48, 100.8, 101.0, 100.5, 100.9),  # retry fails too
    ]
    summary = run(runner, broker, bars)  # returns — never raises
    types = event_types(events)
    assert "exit_failure_critical" in types
    assert "exit_pending_retry" in types
    # Strategy exit + per-bar retry + end-of-data flatten: the
    # exit was attempted (and escalated) repeatedly, in-run.
    assert summary["exit_failures"] >= 2
    assert broker.exit_attempts >= 2
    assert broker.close_attempts >= 2
    assert "flatten_failed_position_carried" in types
    # Nothing was booked; the position is intact in BOTH ledgers.
    assert summary["trades"] == 0
    assert broker.position_qty("SPY") == 93
    assert store.load()["position"] is not None


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


def test_fixed_F4_partial_exit_remainder_stays_under_level_management():
    """FIXED (audit F4, hardening 2026-10-21): after a partial exit
    fill, the remaining shares stay under stop/target management
    for the rest of the day. The 10:00 bar that smashes the
    original stop exits the 53-share remainder THERE — not at the
    end of the data."""
    broker = PartialExitBroker()
    runner, events, _ = make_runner(broker)
    bars = range_bars() + [
        mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6),   # entry 93
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),  # target: exit 40
        mkbar(DAY, 10, 0, 100.0, 100.5, 90.0, 95.0),   # stop smashed
        mkbar(DAY, 15, 30, 95.0, 95.5, 94.5, 95.0),
    ]
    summary = run(runner, broker, bars)
    exits = [o for o in broker.orders if o["side"] == "sell"]
    assert [(o["qty"]) for o in exits] == [40, 53]
    types = event_types(events)
    assert "exit_partial_remainder" in types
    assert "flatten_enforced" not in types  # remainder exited at 10:00
    closed = [e for e in events if e["type"] == "trade_closed"]
    assert [c["exit_reason"] for c in closed] == ["TARGET", "STOP"]
    assert closed[1]["managed_by"] == "runner_remainder_levels"
    assert summary["trades"] == 2  # two booked exit legs
    # 40 shares at the target bar's close, 53 at the stop bar's.
    assert summary["realized_pnl_usd"] == round(
        40 * (103.9 - 100.6) + 53 * (95.0 - 100.6), 2
    )
    assert broker.position_qty("SPY") == 0


# ======================================================================
# 2. Sizing
# ======================================================================
def test_fixed_F5_sizing_is_capped_and_the_clipping_is_logged():
    """FIXED (audit F5, hardening 2026-10-21): the frozen strategy
    still computes its uncapped size (SPY_META.max_units stays
    None — the spec is untouched), but the RUNNER's hard caps
    (MAX_SHARES=200, MAX_NOTIONAL_USD=$150k) bind before the
    order. The signal event keeps the spec size (the strategy's
    decision); the ORDER goes out at the capped size, the trade
    books at its actual size/risk, and a size_capped event
    records spec vs actual."""
    assert SPY_META.max_units is None
    # Geometry for exactly floor(150 / 0.50) = 300 shares — the
    # worst real-tape shape (2024-12-05 requested ~$182k notional).
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    bars = range_bars(hi=100.49, lo=100.00, close=100.25) + [
        mkbar(DAY, 9, 45, 100.3, 100.55, 100.2, 100.50),
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),
    ]
    summary = run(runner, broker, bars)
    signal = [e for e in events if e["type"] == "signal"]
    assert signal[0]["shares"] == 300        # spec decision unchanged
    assert broker.orders[0]["qty"] == 200    # MAX_SHARES bound it
    capped = [e for e in events if e["type"] == "size_capped"]
    (capped,) = capped
    assert capped["spec_shares"] == 300
    assert capped["capped_shares"] == 200
    assert "max_shares" in capped["binding_caps"]
    assert summary["size_capped"] == 1
    assert summary["trades"] == 1           # booked at actual size

    # The NOTIONAL cap binds when price x spec shares crosses
    # $150k first: 166 spec shares at $1,000 = $166k -> 150 out.
    broker_n = SimulatedBroker()
    runner_n, events_n, _ = make_runner(broker_n)
    bars_n = range_bars(hi=999.90, lo=999.10, close=999.50) + [
        mkbar(DAY, 9, 45, 999.6, 1000.2, 999.4, 1000.00),
    ]
    run(runner_n, broker_n, bars_n)
    signal_n = [e for e in events_n if e["type"] == "signal"]
    assert signal_n[0]["shares"] == 166  # floor(150 / 0.90)
    assert broker_n.orders[0]["qty"] == 150  # floor(150000 / 1000)
    capped_n = [e for e in events_n if e["type"] == "size_capped"]
    assert capped_n[0]["binding_caps"] == ["max_notional"]


def test_fixed_F5_entry_rejection_still_fails_the_day_by_design():
    """KEPT BEHAVIOR (audit F5 scope note): the caps fix the SIZE,
    not the entry-failure policy — a buying-power rejection on
    ENTRY still propagates and fails the day (the task scoped
    containment to exits). The restart then converts the day to
    a missed_day via the live-frontier gate: no second attempt,
    nothing booked on a position that never existed."""
    class BuyingPowerBroker(SimulatedBroker):
        def place_market(self, symbol, qty, side):
            raise AlpacaAPIError(
                "insufficient buying power", status=403
            )

    bars = range_bars(hi=100.49, lo=100.00, close=100.25) + [
        mkbar(DAY, 9, 45, 100.3, 100.55, 100.2, 100.50),
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),
    ]
    broker2 = BuyingPowerBroker()
    runner2, _, store2 = make_runner(broker2)
    with pytest.raises(AlpacaAPIError):
        run(runner2, broker2, bars)
    assert store2.load()["position"] is None

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
def test_fixed_F6_early_close_day_uses_the_calendars_flatten_time():
    """FIXED (audit F6, hardening 2026-10-21): the runner consults
    the market calendar at day start. On a 13:00 early close the
    day's flatten time becomes min(15:55, close − 5 min) = 12:55
    and is on the day-start record — the position no longer
    depends on the accidental timing of the feed ending; the
    schedule is known and logged BEFORE the session starts."""
    assert hasattr(AlpacaClient, "get_calendar")
    broker = SimulatedBroker()
    runner, events, _ = make_runner(
        broker,
        calendar_fn=lambda day: {"open": time(9, 30), "close": time(13, 0)},
    )
    bars = long_entry_bars() + [
        mkbar(DAY, 12, 59, 101.0, 101.2, 100.8, 101.0),
        mkbar(DAY, 13, 0, 101.0, 101.1, 100.9, 101.0),  # last bar
    ]
    summary = run(runner, broker, bars)
    assert summary["trades"] == 1
    assert broker.position_qty("SPY") == 0
    (start,) = [e for e in events if e["type"] == "runner_day_start"]
    assert start["calendar_status"] == "ok"
    assert start["market_close"] == "13:00"
    assert start["early_close"] is True
    assert start["flatten_time"] == "12:55"


def test_fixed_F6_non_trading_day_skips_cleanly():
    """FIXED (audit F6): the calendar says NOT a trading day ->
    the run is a clean skip: no orders, no strategy, an explicit
    non_trading_day on the record."""
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker, calendar_fn=lambda day: None)
    summary = run(runner, broker, long_entry_bars())
    assert summary["trades"] == 0
    assert summary["no_trade_reason"] == "non_trading_day"
    assert summary["halted"] is False
    assert broker.orders == []
    assert "non_trading_day" in event_types(events)


def test_fixed_F6_calendar_failure_falls_back_loudly():
    """FIXED (audit F6): if the calendar fetch FAILS, the runner
    uses the default RTH schedule AND logs calendar_unavailable —
    the fallback is never silent — and the day trades normally."""
    def boom(day):
        raise RuntimeError("calendar endpoint down")

    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker, calendar_fn=boom)
    bars = range_bars() + [
        mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6),
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),
    ]
    summary = run(runner, broker, bars)
    assert summary["trades"] == 1
    assert "calendar_unavailable" in event_types(events)
    (start,) = [e for e in events if e["type"] == "runner_day_start"]
    assert start["calendar_status"] == "unavailable_fallback"
    assert start["flatten_time"] == "15:55"  # default schedule


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
def test_fixed_F7_second_instance_is_refused_by_the_lock(tmp_path, monkeypatch):
    """FIXED (audit F7, hardening 2026-10-21): a non-blocking
    flock on runner.lock in the state dir is the single-instance
    guard. A second acquire fails while the first holds it (and
    succeeds after release) — and main() takes the lock BEFORE
    any API use, so a duplicate process refuses cleanly (exit 0,
    refusal logged) instead of double-trading the account."""
    lock_a = InstanceLock(tmp_path / "runner.lock")
    lock_b = InstanceLock(tmp_path / "runner.lock")
    assert lock_a.acquire() is True
    assert lock_b.acquire() is False  # held -> refused, no waiting
    lock_a.release()
    assert lock_b.acquire() is True   # released -> available again
    lock_b.release()

    import alpaca.runner as runner_mod

    state_dir = tmp_path / "state"
    holder = InstanceLock(state_dir / "runner.lock")
    assert holder.acquire() is True
    built = []

    class FakeClient:
        def __init__(self, config):
            built.append("client")

        def verify_paper(self):
            return {"status": "ACTIVE", "account_number": "PA-TEST"}

    monkeypatch.setattr(runner_mod, "AlpacaClient", FakeClient)
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "PKTEST")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "sekret")
    try:
        # Lock held by "the service": main() refuses, exit 0,
        # and never even builds the API client.
        assert runner_mod.main(["--state-dir", str(state_dir)]) == 0
        assert built == []
        refused = (state_dir / "events.jsonl").read_text()
        assert "runner_lock_refused" in refused
    finally:
        holder.release()
    # Lock free: the same entrypoint proceeds to build the client.
    monkeypatch.setattr(
        runner_mod.Orb15Runner, "serve",
        lambda self, source_factory, **kw: None,
    )
    assert runner_mod.main(["--state-dir", str(state_dir)]) == 0
    assert built == ["client"]


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


def test_fixed_F8_corrupt_state_quarantined_halted_and_reconciled(tmp_path):
    """FIXED (audit F8, hardening 2026-10-21): a corrupt state file
    no longer crash-loops the service. run_day quarantines the
    file (renamed runner_state.corrupt-<ts>, never deleted), logs
    state_quarantined loudly, and starts HALTED pending
    reconciliation: a flat account stays halted for review; a
    HOLDING account goes through the F1 orphan path (flattened,
    still halted). run_day RETURNS in both cases — no exception,
    no loop."""
    # Case 1: flat account.
    path = tmp_path / "runner_state.json"
    path.write_bytes(b'{"date": "2026-10-21", "position": {"dire')
    store = StateStore(path)
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker, store=store)
    summary = runner.run_day(DAY, [])  # returns; used to raise
    assert summary["halted"] is True
    types = event_types(events)
    assert "state_quarantined" in types
    assert "quarantine_reconciled_flat" in types
    corrupt = list(tmp_path.glob("runner_state.corrupt-*.json"))
    assert len(corrupt) == 1
    assert corrupt[0].read_bytes().startswith(b'{"date"')
    assert store.load()["halted"] is True  # fresh state is valid JSON

    # Case 2: the account is HOLDING when the state corrupts —
    # the orphan policy flattens it during the halted start.
    path2 = tmp_path / "s2" / "runner_state.json"
    path2.parent.mkdir(parents=True)
    path2.write_bytes(b"garbage{")
    store2 = StateStore(path2)
    broker2 = SimulatedBroker()
    broker2.seed_position(50)
    broker2.set_price(101.0)
    runner2, events2, _ = make_runner(broker2, store=store2)
    summary2 = runner2.run_day(DAY, [])
    assert summary2["halted"] is True
    assert broker2.position_qty("SPY") == 0  # flattened, not riding
    types2 = event_types(events2)
    assert "state_quarantined" in types2
    assert "orphan_flattened" in types2


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


def test_feed_exception_aborts_run_recovery_is_restart_plus_backstop():
    """KEPT BEHAVIOR within the F11 fix (audit F11, hardening
    2026-10-21): a feed EXCEPTION still aborts run_day — no
    flatten is attempted by the dying run itself. That path's
    recovery is the process restart (systemd) + reconciliation
    adoption/overnight flatten, unchanged. What the hardening
    adds is for the OTHER failure shape — the feed that STALLS
    without raising, where the process lives but bars stop: see
    the backstop tests below."""
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


def _backstop_ready_runner(broker):
    """A runner frozen mid-run: position open, day context live,
    flatten time 15:55, last bar processed 15:50."""
    runner, events, store = make_runner(broker)
    broker.seed_position(93)
    broker.set_price(102.0)
    runner._pos = RunnerPosition(
        direction="long", qty=93, entry_price=100.6,
        strategy_entry_price=100.6, entry_time="09:45",
        stop=99.0, target=103.8, entry_order_id="sim-seed",
    )
    runner._current_day = DAY
    state = {
        "version": 1, "date": DAY.isoformat(), "phase": "IN_POSITION",
        "position": runner._pos.to_state(),
        "overlay_equity_usd": 500.0, "realized_pnl_total_usd": 0.0,
        "halted": False, "halt_reason": None,
    }
    summary = {
        "date": DAY.isoformat(), "halted": False, "trades": 0,
        "realized_pnl_usd": 0.0, "realized_pnl_af_model_usd": 0.0,
        "overlay_equity_usd": 500.0,
    }
    runner._day_state = state
    runner._day_summary = summary
    runner._flatten_time = time(15, 55)
    runner._day_flatten_dt = datetime(2026, 10, 21, 15, 55, tzinfo=ET)
    runner._last_bar_at = datetime(2026, 10, 21, 15, 50, tzinfo=ET)
    return runner, events, state, summary


def test_fixed_F11_backstop_flattens_when_the_feed_stalls():
    """FIXED (audit F11, contained version): the serve loop's
    watchdog checks the WALL CLOCK: position open + past the
    day's flatten time + no bar processed for over 3 minutes ->
    flatten through the normal exit path and log
    flatten_backstop_fired. A stalled (not crashed) feed can no
    longer leave a position riding past the close."""
    broker = SimulatedBroker()
    runner, events, state, summary = _backstop_ready_runner(broker)
    now = datetime(2026, 10, 21, 16, 5, tzinfo=ET)  # 15 min stale
    assert runner._maybe_backstop_flatten(now) is True
    assert broker.position_qty("SPY") == 0
    assert runner._pos is None
    types = event_types(events)
    assert "flatten_backstop_fired" in types
    closed = [e for e in events if e["type"] == "trade_closed"]
    assert closed[0]["managed_by"] == "wall_clock_backstop"
    assert closed[0]["exit_reason"] == "EOD_FLATTEN"
    assert summary["trades"] == 1
    assert state["overlay_equity_usd"] == round(500 + 93 * (102.0 - 100.6), 2)


def test_fixed_F11_backstop_stays_quiet_while_bars_are_fresh():
    """FIXED (audit F11): the backstop's conditions are conjunct-
    ive — a fresh bar (<= 3 min) OR a not-yet-reached flatten
    time means it does NOT fire. It is a backstop, not a second
    exit engine."""
    broker = SimulatedBroker()
    runner, events, state, summary = _backstop_ready_runner(broker)
    # 1 minute stale, past flatten: too fresh to conclude a stall.
    runner._last_bar_at = datetime(2026, 10, 21, 15, 57, tzinfo=ET)
    assert runner._maybe_backstop_flatten(
        datetime(2026, 10, 21, 15, 58, tzinfo=ET)
    ) is False
    # Very stale, but BEFORE the flatten time: not its business.
    runner._last_bar_at = datetime(2026, 10, 21, 12, 0, tzinfo=ET)
    assert runner._maybe_backstop_flatten(
        datetime(2026, 10, 21, 15, 0, tzinfo=ET)
    ) is False
    assert broker.position_qty("SPY") == 93
    assert "flatten_backstop_fired" not in event_types(events)


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


def test_fixed_F13_transport_refuses_redirects_off_the_locked_host():
    """FIXED (audit F13, hardening 2026-10-21): urllib_transport
    now refuses redirects outright (the Databento helper's
    NoRedirect pattern). A 302 raises AlpacaAPIError and the
    redirect target is NEVER contacted — the credential headers
    cannot leave the locked host."""
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
        with pytest.raises(AlpacaAPIError, match="redirect refused"):
            urllib_transport(
                "GET",
                f"http://127.0.0.1:{redir_port}/v2/account",
                {"APCA-API-KEY-ID": "PKTEST",
                 "APCA-API-SECRET-KEY": "sekret"},
                None,
            )
    finally:
        target.shutdown()
        redirector.shutdown()
    # The redirect target was never contacted at all.
    assert hits == {}


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
