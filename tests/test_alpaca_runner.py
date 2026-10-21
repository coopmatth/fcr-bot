"""Mocked tests for the ORB-15 Alpaca PAPER forward-test runner.

Run from the project root:  python3 -m pytest tests/ -q

NO real network calls and no real credentials: the broker is the
SimulatedBroker (fills at the last bar close via priced_bars) or a
stub client. Days are crafted bar-by-bar so the strategy outcomes
are known exactly — the sizing/geometry expectations mirror
ORB15_SPEC.md (and its 2024-10-21 example geometry: a $0.59 stop
distance at a $150 budget is 254 shares).
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from alpaca.runner import (
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


def long_target_day():
    """Range 100/99; 09:45 close 100.6 -> long 93 sh (stop 99,
    target 103.8); 09:47 bar touches target, closing 103.9."""
    return range_bars() + [
        mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6),
        mkbar(DAY, 9, 46, 100.6, 101.0, 100.2, 100.8),
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),
    ]


# ----------------------------------------------------------------------
# full-day simulations
# ----------------------------------------------------------------------
def test_long_breakout_target_exit_books_fill_prices():
    broker = SimulatedBroker()
    runner, events, store = make_runner(broker)
    summary = run(runner, broker, long_target_day())

    assert summary["trades"] == 1
    assert summary["halted"] is False
    # Fills, not levels: entry at the trigger close 100.6, exit at
    # the exit bar's close 103.9 (the strategy's level was 103.8).
    assert summary["realized_pnl_usd"] == round(93 * (103.9 - 100.6), 2)
    assert summary["overlay_equity_usd"] == round(500 + 93 * 3.3, 2)
    assert [(o["side"], o["qty"]) for o in broker.orders] == [
        ("buy", 93), ("sell", 93),
    ]
    closed = [e for e in events if e["type"] == "trade_closed"]
    (closed,) = closed
    assert closed["exit_reason"] == "TARGET"
    assert closed["strategy_exit_price"] == pytest.approx(103.8)
    assert closed["fill_exit_price"] == pytest.approx(103.9)
    signal = [e for e in events if e["type"] == "signal"]
    (signal,) = signal
    assert signal["direction"] == "long"
    assert signal["shares"] == 93
    assert signal["stop"] == pytest.approx(99.0)
    # State persisted flat, equity carried.
    state = store.load()
    assert state["position"] is None
    assert state["overlay_equity_usd"] == summary["overlay_equity_usd"]
    assert state["phase"] == "DONE"
    assert broker.position_qty("SPY") == 0


def test_short_breakout_stop_exit():
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    bars = range_bars() + [
        mkbar(DAY, 9, 45, 99.2, 99.4, 98.2, 98.4),   # short @ 98.4
        mkbar(DAY, 9, 46, 98.6, 100.3, 98.5, 100.1),  # high hits stop 100
    ]
    summary = run(runner, broker, bars)
    assert summary["trades"] == 1
    assert summary["realized_pnl_usd"] == round(-93 * (100.1 - 98.4), 2)
    assert [(o["side"], o["qty"]) for o in broker.orders] == [
        ("sell", 93), ("buy", 93),
    ]
    closed = [e for e in events if e["type"] == "trade_closed"]
    assert closed[0]["exit_reason"] == "STOP"
    assert closed[0]["strategy_exit_price"] == pytest.approx(100.0)
    assert closed[0]["fill_exit_price"] == pytest.approx(100.1)


def test_no_breakout_day_no_orders():
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    bars = range_bars()
    t = datetime(2026, 10, 21, 9, 45, tzinfo=ET)
    while t.time() < time(16, 0):
        bars.append(mkbar(DAY, t.hour, t.minute, 99.5, 99.9, 99.1, 99.5))
        t += timedelta(minutes=1)
    summary = run(runner, broker, bars)
    assert summary["trades"] == 0
    assert summary["no_trade_reason"] == "no_breakout"
    assert broker.orders == []
    assert summary["overlay_equity_usd"] == 500.0


def test_zero_share_skip_day():
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    # Range 200/50; breakout close 201 -> stop distance 151 > $150.
    bars = range_bars(hi=200.0, lo=50.0, close=125.0) + [
        mkbar(DAY, 9, 45, 150.0, 202.0, 149.0, 201.0),
        mkbar(DAY, 9, 46, 201.0, 202.0, 199.0, 200.0),
    ]
    summary = run(runner, broker, bars)
    assert summary["trades"] == 0
    assert summary["skipped_for_size"] == 1
    assert summary["no_trade_reason"] == "skipped_for_size"
    assert broker.orders == []
    assert "signal_skipped_size" in event_types(events)


def test_flatten_at_1555_bar():
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    bars = range_bars() + [mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6)]
    t = datetime(2026, 10, 21, 9, 46, tzinfo=ET)
    while t.time() < time(15, 55):
        bars.append(mkbar(DAY, t.hour, t.minute, 101.0, 101.3, 100.7, 101.0))
        t += timedelta(minutes=1)
    bars.append(mkbar(DAY, 15, 55, 101.0, 101.2, 100.8, 101.1))
    summary = run(runner, broker, bars)
    assert summary["trades"] == 1
    closed = [e for e in events if e["type"] == "trade_closed"]
    (closed,) = closed
    assert closed["exit_reason"] == "EOD_FLATTEN"
    # Strategy flattens at the 15:55 bar's OPEN (101.0); the runner's
    # market fill is that bar's close (101.1) — fills, not levels.
    assert closed["strategy_exit_price"] == pytest.approx(101.0)
    assert closed["fill_exit_price"] == pytest.approx(101.1)
    assert summary["realized_pnl_usd"] == round(93 * (101.1 - 100.6), 2)


def test_phases_progress_through_the_day():
    broker = SimulatedBroker()
    runner, _, _ = make_runner(broker)
    bars = range_bars() + [
        mkbar(DAY, 9, 45, 99.5, 99.9, 99.1, 99.5),    # in range: ARMED
        mkbar(DAY, 9, 46, 99.8, 100.7, 99.7, 100.6),  # trigger: IN_POSITION
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),  # target: DONE
    ]
    seen = {}

    def probe():
        for i, bar in enumerate(priced_bars(bars, broker)):
            yield bar
            seen[i] = runner._phase

    summary = runner.run_day(DAY, probe())
    assert seen[14] == "RANGE_BUILDING"   # last range bar processed
    assert seen[15] == "ARMED"            # range final, no trigger yet
    assert seen[16] == "IN_POSITION"
    assert summary["trades"] == 1


# ----------------------------------------------------------------------
# sizing (spec examples) + kill switch
# ----------------------------------------------------------------------
def test_sizing_matches_spec_example_geometry():
    # ORB15_SPEC validation-day geometry: stop distance $0.59 at a
    # $150 budget -> floor(150 / 0.59) = 254 shares. That is the
    # STRATEGY's decision and the signal event still records it —
    # but since the 2026-10-21 hardening (audit F5) the runner's
    # hard cap MAX_SHARES=200 binds before the order, so the order
    # itself is 200 and a size_capped event records the clipping.
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    bars = range_bars(hi=100.58, lo=100.00, close=100.29) + [
        mkbar(DAY, 9, 45, 100.3, 100.7, 100.2, 100.59),
        mkbar(DAY, 9, 46, 100.59, 100.6, 100.5, 100.59),
    ]
    run(runner, broker, bars)
    signal = [e for e in events if e["type"] == "signal"]
    (signal,) = signal
    assert signal["shares"] == 254
    assert broker.orders[0]["qty"] == 200
    assert broker.orders[0]["side"] == "buy"
    capped = [e for e in events if e["type"] == "size_capped"]
    (capped,) = capped
    assert capped["spec_shares"] == 254
    assert capped["capped_shares"] == 200


def test_kill_switch_blocks_orders_but_logs_signal():
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker, enabled=False)
    summary = run(runner, broker, long_target_day())
    assert broker.orders == []
    assert summary["orders_blocked_kill_switch"] == 1
    assert summary["trades"] == 0
    assert summary["overlay_equity_usd"] == 500.0
    types = event_types(events)
    assert "signal" in types
    assert "order_blocked_kill_switch" in types
    # The strategy still ran its course, observed only.
    assert "strategy_trade_closed_observe_only" in types


# ----------------------------------------------------------------------
# restart: adoption, orphan management, halts, overnight leftover
# ----------------------------------------------------------------------
def _state_with_position(day=DAY, **overrides):
    position = {
        "direction": "long", "qty": 93, "entry_price": 100.6,
        "strategy_entry_price": 100.6, "entry_time": "09:45",
        "stop": 99.0, "target": 103.8, "entry_order_id": "sim-seed",
    }
    position.update(overrides)
    return {
        "version": 1, "date": day.isoformat(), "phase": "IN_POSITION",
        "position": position, "overlay_equity_usd": 500.0,
        "realized_pnl_total_usd": 0.0, "halted": False,
        "halt_reason": None,
    }


def test_restart_mid_position_adopts_and_continues():
    broker = SimulatedBroker()
    broker.seed_position(93)
    store = StateStore()
    store.save(_state_with_position())
    runner, events, _ = make_runner(broker, store=store)
    summary = run(runner, broker, long_target_day())
    assert summary["halted"] is False
    assert summary["trades"] == 1
    # Only the exit order was placed — the entry was adopted, not re-ordered.
    assert [(o["side"], o["qty"]) for o in broker.orders] == [("sell", 93)]
    assert "entry_adopted_after_restart" in event_types(events)
    assert summary["overlay_equity_usd"] == round(500 + 93 * 3.3, 2)


def test_restart_with_feed_gap_orphan_flattens_at_1555():
    broker = SimulatedBroker()
    broker.seed_position(93)
    store = StateStore()
    store.save(_state_with_position())
    runner, events, _ = make_runner(broker, store=store)
    # The feed only has bars from 15:56: the strategy can never
    # rebuild the position, so the runner manages it from the
    # recorded levels — and the first bar is already past 15:55.
    bars = [mkbar(DAY, 15, 56, 101.0, 101.1, 100.9, 101.0),
            mkbar(DAY, 15, 57, 101.0, 101.1, 100.9, 101.0)]
    summary = run(runner, broker, bars)
    assert summary["halted"] is False
    assert summary["trades"] == 1
    assert [(o["side"], o["qty"]) for o in broker.orders] == [("sell", 93)]
    closed = [e for e in events if e["type"] == "trade_closed"]
    (closed,) = closed
    assert closed["exit_reason"] == "EOD_FLATTEN"
    assert closed["managed_by"] == "runner_orphan_levels"
    assert summary["realized_pnl_usd"] == round(93 * (101.0 - 100.6), 2)


def test_reconciliation_halt_unknown_broker_position():
    # ORPHAN POLICY (hardening F1, 2026-10-21): an account holding
    # the state file cannot explain is FLATTENED via close_position
    # (never left riding, never adopted on a guess), logged as
    # orphan_flattened — and the day still halts for entries.
    broker = SimulatedBroker()
    broker.seed_position(50)  # the account holds SPY; state knows nothing
    runner, events, store = make_runner(broker)
    summary = run(runner, broker, long_target_day())
    assert summary["halted"] is True
    assert "orphan policy" in summary["halt_reason"]
    assert broker.position_qty("SPY") == 0  # flattened, not left open
    closes = [o for o in broker.orders if o.get("kind") == "close_position"]
    assert len(closes) == 1 and closes[0]["qty"] == 50
    assert store.load()["halted"] is True
    types = event_types(events)
    assert "orphan_detected" in types
    assert "orphan_flattened" in types
    assert "reconciliation_halt" in types


def test_reconciliation_halt_state_position_missing_at_broker():
    broker = SimulatedBroker()  # flat, but state claims a position
    store = StateStore()
    store.save(_state_with_position())
    runner, events, _ = make_runner(broker, store=store)
    summary = run(runner, broker, long_target_day())
    assert summary["halted"] is True
    assert "vanished" in summary["halt_reason"]
    assert broker.orders == []


def test_reconciliation_halt_qty_mismatch():
    broker = SimulatedBroker()
    broker.seed_position(50)
    store = StateStore()
    store.save(_state_with_position())  # state says 93
    runner, _, _ = make_runner(broker, store=store)
    summary = run(runner, broker, long_target_day())
    assert summary["halted"] is True
    assert "mismatch" in summary["halt_reason"]


def test_halted_state_stays_halted_next_day():
    broker = SimulatedBroker()
    broker.seed_position(50)
    runner, _, store = make_runner(broker)
    assert run(runner, broker, long_target_day())["halted"] is True
    broker2 = SimulatedBroker()
    runner2, events2, _ = make_runner(broker2, store=store)
    summary2 = run(runner2, broker2, long_target_day(),
                   day=date(2026, 10, 22))
    assert summary2["halted"] is True
    assert broker2.orders == []
    assert "runner_start_halted" in event_types(events2)


def test_overnight_leftover_flattened_and_booked():
    broker = SimulatedBroker()
    broker.seed_position(93)
    broker.set_price(101.0)  # the flatten fills at the current market
    store = StateStore()
    store.save(_state_with_position(day=date(2026, 10, 20)))
    runner, events, _ = make_runner(broker, store=store)
    bars = range_bars() + [mkbar(DAY, 9, 45, 99.5, 99.9, 99.1, 99.5)]
    summary = run(runner, broker, bars)
    assert summary["halted"] is False
    types = event_types(events)
    assert "overnight_leftover_detected" in types
    assert "overnight_leftover_flattened" in types
    # Booked into the overlay: +93 x (101.0 - 100.6).
    assert summary["overlay_equity_usd"] == round(500 + 93 * 0.4, 2)
    assert broker.position_qty("SPY") == 0


# ----------------------------------------------------------------------
# AlpacaBroker over a stub client
# ----------------------------------------------------------------------
class StubClient:
    def __init__(self, polls):
        self._polls = list(polls)
        self.placed = []

    def place_market_order(self, symbol, qty, side):
        self.placed.append((symbol, qty, side))
        return {"id": "ord-9", "status": "accepted"}

    def get_order(self, order_id):
        return self._polls.pop(0)

    def get_position(self, symbol):
        return {"symbol": "SPY", "qty": "-254"}


def test_alpaca_broker_polls_to_fill():
    client = StubClient([
        {"id": "ord-9", "status": "accepted", "filled_qty": "0",
         "filled_avg_price": None},
        {"id": "ord-9", "status": "filled", "filled_qty": "254",
         "filled_avg_price": "584.40"},
    ])
    broker = AlpacaBroker(client, sleep_fn=lambda s: None)
    fill = broker.place_market("SPY", 254, "buy")
    assert fill == Fill(price=584.40, qty=254, order_id="ord-9")
    assert client.placed == [("SPY", 254, "buy")]


def test_alpaca_broker_position_qty_signed():
    broker = AlpacaBroker(StubClient([]), sleep_fn=lambda s: None)
    assert broker.position_qty("SPY") == -254


def test_alpaca_broker_rejection_raises():
    client = StubClient([
        {"id": "ord-9", "status": "rejected", "filled_qty": "0",
         "filled_avg_price": None},
    ])
    broker = AlpacaBroker(client, sleep_fn=lambda s: None)
    with pytest.raises(OrderError):
        broker.place_market("SPY", 10, "buy")


def test_alpaca_broker_unfilled_raises_not_assumes():
    client = StubClient([
        {"id": "ord-9", "status": "accepted", "filled_qty": "0",
         "filled_avg_price": None},
    ] * 3)
    broker = AlpacaBroker(client, sleep_fn=lambda s: None, max_polls=3)
    with pytest.raises(OrderError):
        broker.place_market("SPY", 10, "buy")


# ----------------------------------------------------------------------
# AlpacaBarSource: completed-bar polling
# ----------------------------------------------------------------------
def test_alpaca_bar_source_yields_completed_bars_then_stops():
    raw = [
        Bar(ts=datetime(2026, 10, 21, 9, 30, tzinfo=ET),
            open=100, high=101, low=99, close=100.5, volume=1),
        Bar(ts=datetime(2026, 10, 21, 9, 31, tzinfo=ET),
            open=100.5, high=101, low=100, close=100.7, volume=1),
        Bar(ts=datetime(2026, 10, 21, 15, 59, tzinfo=ET),
            open=100, high=101, low=99, close=100.2, volume=1),
    ]

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def get_bars(self, symbol, start, end, feed="iex"):
            self.calls += 1
            return raw

    times = iter([
        datetime(2026, 10, 21, 9, 32, tzinfo=ET),   # 09:30+09:31 complete
        datetime(2026, 10, 21, 16, 1, tzinfo=ET),   # after close: stop
    ])
    client = FakeClient()
    source = AlpacaBarSource(
        client, clock=lambda: next(times), sleep_fn=lambda s: None,
    )
    bars = list(source.bars_for_day(DAY))
    # The 15:59 bar is only "complete" at 16:00 — by the second poll
    # (16:01) it is, so all three arrive, then iteration stops.
    assert [b.ts.minute for b in bars] == [30, 31, 59]
    assert client.calls == 2


# ----------------------------------------------------------------------
# live-frontier gate: a fresh start mid-session must never trade the
# past (the 2026-10-21 deploy-day bug: started 14:32 ET, the runner
# replayed the morning backfill and EXECUTED its 09:47 signal live).
# ----------------------------------------------------------------------
def run_frontier(runner, broker, bars, live_since, day=DAY):
    return runner.run_day(
        day, priced_bars(bars, broker), live_since=live_since
    )


def test_midday_start_resolved_trade_becomes_missed_day():
    """(a) Fresh 14:32 start; the morning's trade (09:45 entry,
    09:47 target) already resolved inside the backfill: NO orders,
    a missed_day carrying the replayed outcome, overlay untouched,
    phase DONE."""
    broker = SimulatedBroker()
    runner, events, store = make_runner(broker)
    bars = long_target_day() + [
        mkbar(DAY, 15, 0, 99.5, 99.9, 99.1, 99.5),
        mkbar(DAY, 15, 1, 99.5, 99.9, 99.1, 99.5),
    ]
    summary = run_frontier(
        runner, broker, bars, datetime(2026, 10, 21, 14, 32, tzinfo=ET)
    )
    assert broker.orders == []
    assert summary["trades"] == 0
    assert summary["missed_day"] is True
    assert summary["no_trade_reason"] == "missed_day"
    assert summary["overlay_equity_usd"] == 500.0
    assert summary["catchup_bars"] > 0
    types = event_types(events)
    assert "catchup_signal" in types
    assert "signal" not in types          # no live signal was logged
    assert "order_filled" not in types
    (missed_ev,) = [e for e in events if e["type"] == "missed_day"]
    assert missed_ev["outcome"] == "closed_in_replay"
    assert missed_ev["direction"] == "long"
    assert missed_ev["shares"] == 93
    assert missed_ev["exit_reason"] == "TARGET"
    # The strategy's replayed outcome, for the record: level fill
    # 103.8 vs entry 100.6, and the AF-model version net of friction.
    assert missed_ev["strategy_pnl_usd"] == pytest.approx(93 * 3.2)
    assert missed_ev["strategy_pnl_af_model_usd"] == pytest.approx(
        93 * 3.2 - 0.04 * 93
    )
    assert missed_ev["booked"] is False
    state = store.load()
    assert state["phase"] == "DONE"
    assert state["position"] is None
    assert state["overlay_equity_usd"] == 500.0
    assert state["live_since"].startswith("2026-10-21T14:32")
    assert state["missed_day"]["outcome"] == "closed_in_replay"
    assert broker.position_qty("SPY") == 0


def test_midday_start_open_trade_at_frontier_is_missed_not_entered():
    """(a2) Fresh 10:00 start; the 09:45 entry's trade is still
    open when the first live bar arrives: missed_day is declared
    at the frontier (marked at the last catch-up close), the trade
    is NEVER entered late, and its later simulated flatten books
    nothing."""
    broker = SimulatedBroker()
    runner, events, store = make_runner(broker)
    bars = range_bars() + [mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6)]
    t = datetime(2026, 10, 21, 9, 46, tzinfo=ET)
    while t.time() < time(15, 55):
        bars.append(mkbar(DAY, t.hour, t.minute, 101.0, 101.3, 100.7, 101.0))
        t += timedelta(minutes=1)
    bars.append(mkbar(DAY, 15, 55, 101.0, 101.2, 100.8, 101.1))
    summary = run_frontier(
        runner, broker, bars, datetime(2026, 10, 21, 10, 0, tzinfo=ET)
    )
    assert broker.orders == []
    assert summary["trades"] == 0
    assert summary["missed_day"] is True
    assert summary["overlay_equity_usd"] == 500.0
    (missed_ev,) = [e for e in events if e["type"] == "missed_day"]
    assert missed_ev["outcome"] == "open_at_frontier"
    # Marked at the last catch-up close (the 09:59 bar, close 101.0).
    assert missed_ev["mark_price"] == pytest.approx(101.0)
    assert missed_ev["strategy_unrealized_usd"] == pytest.approx(
        93 * (101.0 - 100.6)
    )
    # The simulated trade still ran its course, observed only.
    assert "strategy_trade_closed_observe_only" in event_types(events)
    assert store.load()["missed_day"]["outcome"] == "open_at_frontier"


def test_midday_start_live_bar_signal_executes_normally():
    """(b) Fresh 09:40 start: the range bars are catch-up, but the
    09:45 breakout bar completes AFTER the frontier — a live
    signal, filled normally."""
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    summary = run_frontier(
        runner, broker, long_target_day(),
        datetime(2026, 10, 21, 9, 40, tzinfo=ET),
    )
    assert [(o["side"], o["qty"]) for o in broker.orders] == [
        ("buy", 93), ("sell", 93),
    ]
    assert summary["trades"] == 1
    assert summary["missed_day"] is False
    assert summary["realized_pnl_usd"] == round(93 * 3.3, 2)
    assert summary["overlay_equity_usd"] == round(500 + 93 * 3.3, 2)
    # Bars 09:30-09:39 were already complete at the 09:40 start.
    assert summary["catchup_bars"] == 10
    types = event_types(events)
    assert "signal" in types
    assert "catchup_signal" not in types
    assert "missed_day" not in types
    assert "live_frontier_reached" in types


def test_preopen_start_is_a_normal_live_day():
    """(c) Start before the open (the normal overnight case): the
    frontier precedes every bar, nothing is catch-up, the day
    trades exactly like the un-frontiered baseline."""
    broker = SimulatedBroker()
    runner, events, _ = make_runner(broker)
    summary = run_frontier(
        runner, broker, long_target_day(),
        datetime(2026, 10, 21, 9, 20, tzinfo=ET),
    )
    assert [(o["side"], o["qty"]) for o in broker.orders] == [
        ("buy", 93), ("sell", 93),
    ]
    assert summary["trades"] == 1
    assert summary["catchup_bars"] == 0
    assert summary["missed_day"] is False
    assert summary["overlay_equity_usd"] == round(500 + 93 * 3.3, 2)


def test_restart_adoption_ignores_frontier_for_exits():
    """(d) Restart with a saved state + live position, started
    mid-afternoon: reconciliation/adoption is UNCHANGED by the
    gate — the re-fired entry is adopted (not re-ordered) and the
    exit decided on a historical bar still places its order,
    because the position is real."""
    broker = SimulatedBroker()
    broker.seed_position(93)
    store = StateStore()
    store.save(_state_with_position())
    runner, events, _ = make_runner(broker, store=store)
    summary = run_frontier(
        runner, broker, long_target_day(),
        datetime(2026, 10, 21, 14, 32, tzinfo=ET),
    )
    assert summary["halted"] is False
    assert summary["trades"] == 1
    assert [(o["side"], o["qty"]) for o in broker.orders] == [("sell", 93)]
    assert "entry_adopted_after_restart" in event_types(events)
    assert "missed_day" not in event_types(events)
    assert summary["overlay_equity_usd"] == round(500 + 93 * 3.3, 2)


def test_entry_order_gate_refuses_historical_bar():
    """The gate lives in the entry placement itself: even a direct
    call with a historical bar raises instead of ordering."""
    broker = SimulatedBroker()
    runner, _, _ = make_runner(broker)
    runner._live_since = datetime(2026, 10, 21, 14, 32, tzinfo=ET)
    historical = mkbar(DAY, 9, 47, 771.0, 772.0, 770.0, 771.25)
    with pytest.raises(RuntimeError, match="live-frontier gate"):
        runner._place_entry_order(historical, 100, "buy")
    assert broker.orders == []
    live = mkbar(DAY, 14, 33, 774.0, 774.5, 773.5, 774.27)
    fill = runner._place_entry_order(live, 100, "buy")
    assert fill.qty == 100
    assert [(o["side"], o["qty"]) for o in broker.orders] == [("buy", 100)]
