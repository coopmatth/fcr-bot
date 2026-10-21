"""Mocked tests for the ORB-15 OPTIONS variant (selection, premium
gate, broker, day runner).

Run from the project root:  python3 -m pytest tests/ -q

NO real network calls and no real credentials: clients run on
FakeTransport, days run on SimulatedOptionsBroker. The spec is
ORB15_OPTIONS_SPEC.md: frozen ORB-15 signals; long -> buy CALL,
short -> buy PUT; nearest expiry, ATM strike; exactly 1 contract;
premium gate ask <= $2.50/share with a 15-minute staleness bound;
exits driven by the UNDERLYING stop/target/flatten; $500 overlay
books realized options P&L from actual premiums.
"""
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from alpaca.config import DATA_BASE_URL, PAPER_BASE_URL, AlpacaConfig
from alpaca.options_broker import (
    OptionsBroker,
    OptionsClient,
    QuoteUnavailableError,
    SimulatedOptionsBroker,
    StaleQuoteError,
    priced_option_bars,
    quote_from_snapshot,
)
from alpaca.options_runner import Orb15OptionsRunner
from alpaca.options_select import (
    OptionContract,
    contract_from_api,
    parse_occ_symbol,
    select_contract,
)
from alpaca.runner import EventLog, StateStore
from fcr.patterns import Bar

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
DAY = date(2026, 10, 21)
TOMORROW = date(2026, 10, 22)
YESTERDAY = date(2026, 10, 20)
# The runner's fixed clock for day tests: just after the 09:45
# signal bar. Fresh quotes are stamped 13:44Z (09:44 ET).
CLOCK = datetime(2026, 10, 21, 9, 46, tzinfo=ET)
FRESH_QUOTE_TS = datetime(2026, 10, 21, 13, 44, tzinfo=UTC)

FAKE_ENV = {
    "ALPACA_PAPER_KEY_ID": "PKFAKEPAPERKEYID",
    "ALPACA_PAPER_SECRET": "fake-paper-secret",
}


def occ(underlying, expiry, letter, strike):
    yy = expiry.year % 100
    return (
        f"{underlying}{yy:02d}{expiry.month:02d}{expiry.day:02d}"
        f"{letter}{int(round(strike * 1000)):08d}"
    )


def mkcontract(strike, otype="call", expiry=DAY, tradable=True):
    return OptionContract(
        symbol=occ("SPY", expiry, "C" if otype == "call" else "P", strike),
        underlying="SPY",
        option_type=otype,
        expiry=expiry,
        strike=float(strike),
        tradable=tradable,
    )


def mkbar(day, hh, mm, o, h, l, c):
    return Bar(
        ts=datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET),
        open=o, high=h, low=l, close=c, volume=10000.0,
    )


def range_bars(day=DAY, hi=100.0, lo=99.0, close=99.5):
    return [mkbar(day, 9, 30 + i, close, hi, lo, close) for i in range(15)]


def long_target_day():
    """Range 100/99; 09:45 close 100.6 -> long (stop 99, target
    103.8); 09:47 bar touches the target."""
    return range_bars() + [
        mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6),
        mkbar(DAY, 9, 46, 100.6, 101.0, 100.2, 100.8),
        mkbar(DAY, 9, 47, 100.8, 104.0, 100.5, 103.9),
    ]


def short_stop_day():
    """Range 100/99; 09:45 close 98.4 -> short (stop 100, target
    95.2); 09:46 bar's high 100.3 touches the stop."""
    return range_bars() + [
        mkbar(DAY, 9, 45, 99.2, 99.4, 98.2, 98.4),
        mkbar(DAY, 9, 46, 98.6, 100.3, 98.5, 100.1),
    ]


def event_types(events):
    return [e["type"] for e in events]


# ----------------------------------------------------------------------
# selection: pure frozen rule
# ----------------------------------------------------------------------
def test_parse_occ_symbol_roundtrip_and_rejects_equities():
    call = parse_occ_symbol("SPY261021C00101000")
    assert call.underlying == "SPY"
    assert call.option_type == "call"
    assert call.expiry == DAY
    assert call.strike == 101.0
    put = parse_occ_symbol("SPY261021P00099500")
    assert put.option_type == "put"
    assert put.strike == 99.5
    assert parse_occ_symbol("SPY") is None
    assert parse_occ_symbol("not-a-symbol") is None


def test_contract_from_api_parses_and_rejects_malformed():
    raw = {
        "symbol": "SPY261021C00101000", "type": "call",
        "expiration_date": "2026-10-21", "strike_price": "101.00",
        "underlying_symbol": "SPY", "tradable": True,
    }
    contract = contract_from_api(raw)
    assert contract.strike == 101.0 and contract.expiry == DAY
    assert contract_from_api({"symbol": "X"}) is None
    assert contract_from_api({**raw, "strike_price": "soon"}) is None


def test_select_direction_maps_long_to_call_short_to_put():
    contracts = [mkcontract(100, "call"), mkcontract(100, "put")]
    assert select_contract(contracts, "long", 100.2, DAY).option_type == "call"
    assert select_contract(contracts, "short", 100.2, DAY).option_type == "put"


def test_select_prefers_nearest_expiry_even_at_worse_strike():
    contracts = [
        mkcontract(101, "call", expiry=DAY),       # 0DTE, 0.4 away
        mkcontract(100.5, "call", expiry=TOMORROW)  # tomorrow, 0.1 away
    ]
    chosen = select_contract(contracts, "long", 100.6, DAY)
    assert chosen.expiry == DAY and chosen.strike == 101.0


def test_select_nearest_expiry_when_today_not_listed():
    contracts = [
        mkcontract(100, "call", expiry=TOMORROW),
        mkcontract(100, "call", expiry=date(2026, 10, 28)),
    ]
    chosen = select_contract(contracts, "long", 100.1, DAY)
    assert chosen.expiry == TOMORROW


def test_select_skips_expired_contracts():
    contracts = [
        mkcontract(100.5, "call", expiry=YESTERDAY),  # closest but expired
        mkcontract(102, "call", expiry=DAY),
    ]
    chosen = select_contract(contracts, "long", 100.6, DAY)
    assert chosen.expiry == DAY and chosen.strike == 102.0


def test_select_atm_rounding_both_directions():
    contracts = [mkcontract(100, "call"), mkcontract(101, "call")]
    assert select_contract(contracts, "long", 100.6, DAY).strike == 101.0
    assert select_contract(contracts, "long", 100.4, DAY).strike == 100.0


def test_select_tie_breaks_toward_in_the_money():
    contracts = [mkcontract(100, "call"), mkcontract(101, "call"),
                 mkcontract(100, "put"), mkcontract(101, "put")]
    # Exactly between the strikes: a call takes the lower strike,
    # a put the higher one.
    assert select_contract(contracts, "long", 100.5, DAY).strike == 100.0
    assert select_contract(contracts, "short", 100.5, DAY).strike == 101.0


def test_select_excludes_untradable_and_returns_none_when_empty():
    contracts = [mkcontract(100, "call", tradable=False),
                 mkcontract(103, "call")]
    assert select_contract(contracts, "long", 100.1, DAY).strike == 103.0
    assert select_contract([], "long", 100.0, DAY) is None
    assert select_contract([mkcontract(100, "put")], "long", 100.0, DAY) is None


# ----------------------------------------------------------------------
# client: chain + snapshot endpoints on the locked hosts
# ----------------------------------------------------------------------
class FakeTransport:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers),
                           "body": json.loads(body) if body else None})
        for (m, needle), (status, payload) in self.routes.items():
            if m == method and needle in url:
                return status, {}, json.dumps(payload).encode()
        return 404, {}, json.dumps({"message": "no route"}).encode()


def make_options_client(routes):
    transport = FakeTransport(routes)
    client = OptionsClient(AlpacaConfig.from_env(FAKE_ENV), transport=transport)
    return client, transport


RAW_CALL = {
    "symbol": "SPY261021C00101000", "type": "call",
    "expiration_date": "2026-10-21", "strike_price": "101.00",
    "underlying_symbol": "SPY", "tradable": True, "status": "active",
}


def test_contracts_endpoint_params_and_paper_host():
    client, transport = make_options_client(
        {("GET", "/v2/options/contracts"):
         (200, {"option_contracts": [RAW_CALL], "next_page_token": None})}
    )
    raw = client.list_option_contracts(
        "SPY", option_type="call", on_or_after=DAY, on_or_before=TOMORROW
    )
    assert raw == [RAW_CALL]
    (call,) = transport.calls
    assert call["url"].startswith(f"{PAPER_BASE_URL}/v2/options/contracts?")
    assert "underlying_symbols=SPY" in call["url"]
    assert "type=call" in call["url"]
    assert "expiration_date_gte=2026-10-21" in call["url"]
    assert "expiration_date_lte=2026-10-22" in call["url"]
    assert call["headers"]["APCA-API-KEY-ID"] == "PKFAKEPAPERKEYID"


def test_contracts_pagination_follows_next_page_token():
    pages = iter([
        {"option_contracts": [RAW_CALL], "next_page_token": "tok2"},
        {"option_contracts": [{**RAW_CALL, "symbol": "SPY261021C00102000",
                                "strike_price": "102.00"}],
         "next_page_token": None},
    ])

    def transport(method, url, headers, body):
        return 200, {}, json.dumps(next(pages)).encode()

    client = OptionsClient(AlpacaConfig.from_env(FAKE_ENV),
                           transport=transport)
    raw = client.list_option_contracts("SPY", on_or_after=DAY)
    assert [c["strike_price"] for c in raw] == ["101.00", "102.00"]


SNAPSHOT_ENTRY = {
    "latestQuote": {
        "ap": 1.83, "as": 4, "ax": "C", "bp": 1.79, "bs": 6, "bx": "C",
        "c": ["E", "R"], "t": "2026-10-21T13:44:00.123456789Z",
    },
    "latestTrade": {"p": 1.81, "s": 2, "t": "2026-10-21T13:43:58Z"},
}


def test_snapshot_endpoint_uses_data_host_and_quote_parses():
    client, transport = make_options_client(
        {("GET", "/v1beta1/options/snapshots"):
         (200, {"snapshots": {"SPY261021C00101000": SNAPSHOT_ENTRY}})}
    )
    entry = client.get_option_snapshot("SPY261021C00101000")
    assert entry == SNAPSHOT_ENTRY
    (call,) = transport.calls
    assert call["url"].startswith(f"{DATA_BASE_URL}/v1beta1/options/snapshots?")
    assert "symbols=SPY261021C00101000" in call["url"]
    assert "feed=indicative" in call["url"]
    quote = quote_from_snapshot("SPY261021C00101000", entry)
    assert quote.ask == 1.83 and quote.bid == 1.79
    # The nanosecond quote timestamp parses, truncated to
    # microseconds (datetime's resolution).
    assert quote.ts == datetime(2026, 10, 21, 13, 44, 0, 123456, tzinfo=UTC)


def snapshot_client(entry_by_symbol):
    payload = {"snapshots": entry_by_symbol} if entry_by_symbol is not None \
        else {"snapshots": {}}
    client, _ = make_options_client(
        {("GET", "/v1beta1/options/snapshots"): (200, payload)}
    )
    return OptionsBroker(client, sleep_fn=lambda s: None)


def test_fresh_quote_accepts_fresh_ask():
    broker = snapshot_client({"SPY261021C00101000": SNAPSHOT_ENTRY})
    quote = broker.fresh_quote(
        "SPY261021C00101000", now=CLOCK, max_staleness_minutes=15.0
    )
    assert quote.ask == 1.83


def test_fresh_quote_rejects_stale_missing_and_askless():
    stale = {"latestQuote": {"ap": 1.5, "bp": 1.4,
                             "t": "2026-10-21T13:00:00Z"}}
    broker = snapshot_client({"S": stale})
    with pytest.raises(StaleQuoteError):
        broker.fresh_quote("S", now=CLOCK, max_staleness_minutes=15.0)
    broker = snapshot_client({})
    with pytest.raises(QuoteUnavailableError):
        broker.fresh_quote("S", now=CLOCK, max_staleness_minutes=15.0)
    askless = {"latestQuote": {"ap": 0, "bp": 0, "t": "2026-10-21T13:44:00Z"}}
    broker = snapshot_client({"S": askless})
    with pytest.raises(QuoteUnavailableError):
        broker.fresh_quote("S", now=CLOCK, max_staleness_minutes=15.0)


# ----------------------------------------------------------------------
# day runner over the simulated options broker
# ----------------------------------------------------------------------
def make_options_runner(broker, enabled=True, store=None):
    events = []
    store = store or StateStore()
    runner = Orb15OptionsRunner(
        broker,
        enabled=enabled,
        log=EventLog(sink=events),
        store=store,
        clock=lambda: CLOCK,
    )
    return runner, events, store


def fresh_quote(ask, bid=None, ts=FRESH_QUOTE_TS):
    from alpaca.options_broker import OptionQuote

    return OptionQuote(symbol="", bid=bid, ask=ask, ts=ts)


def standard_chain():
    """Calls and puts around the 99-101 range, today + tomorrow."""
    chain = []
    for expiry in (DAY, TOMORROW):
        for strike in (99, 100, 101, 102):
            chain.append(mkcontract(strike, "call", expiry))
            chain.append(mkcontract(strike, "put", expiry))
    return chain


def run_options(runner, broker, bars, premium_fn, day=DAY):
    return runner.run_day(day, priced_option_bars(bars, broker, premium_fn))


def test_call_win_day_target_exit_books_premium_pnl():
    broker = SimulatedOptionsBroker()
    broker.set_chain(standard_chain())
    # Signal 100.6 -> ATM call is strike 101 (today's expiry).
    symbol = occ("SPY", DAY, "C", 101)
    broker.set_quote(symbol, fresh_quote(ask=1.80, bid=1.78))
    runner, events, store = make_options_runner(broker)
    summary = run_options(
        runner, broker, long_target_day(),
        lambda bar: 3.25 if bar.ts.time() >= time(9, 47) else 1.80,
    )
    assert summary["trades"] == 1
    assert summary["halted"] is False
    assert summary["contract_symbol"] == symbol
    assert summary["premium_paid_usd"] == 180.0
    # (3.25 - 1.80) x 100 x 1 contract.
    assert summary["realized_pnl_usd"] == 145.0
    assert summary["overlay_equity_usd"] == 645.0
    assert [(o["symbol"], o["side"], o["qty"]) for o in broker.orders] == [
        (symbol, "buy", 1), (symbol, "sell", 1),
    ]
    types = event_types(events)
    assert "contract_selected" in types
    assert "premium_gate_passed" in types
    (closed,) = [e for e in events if e["type"] == "trade_closed"]
    assert closed["exit_reason"] == "TARGET"
    assert closed["option_type"] == "call"
    assert closed["entry_premium_usd"] == pytest.approx(1.80)
    assert closed["fill_exit_premium_usd"] == pytest.approx(3.25)
    assert closed["exit_proceeds_usd"] == 325.0
    assert closed["pnl_usd"] == 145.0
    state = store.load()
    assert state["position"] is None
    assert state["overlay_equity_usd"] == 645.0
    assert broker.position_qty(symbol) == 0


def test_put_loss_day_stop_exit_books_negative_pnl():
    broker = SimulatedOptionsBroker()
    broker.set_chain(standard_chain())
    # Signal 98.4 (short) -> ATM put is strike 98? Strikes are
    # 99/100/101/102 in the standard chain; add 98 explicitly.
    broker.set_chain(standard_chain() + [mkcontract(98, "put")])
    symbol = occ("SPY", DAY, "P", 98)
    broker.set_quote(symbol, fresh_quote(ask=2.00, bid=1.96))
    runner, events, _ = make_options_runner(broker)
    summary = run_options(
        runner, broker, short_stop_day(),
        lambda bar: 0.90 if bar.ts.time() >= time(9, 46) else 2.00,
    )
    assert summary["trades"] == 1
    (closed,) = [e for e in events if e["type"] == "trade_closed"]
    assert closed["exit_reason"] == "STOP"
    assert closed["option_type"] == "put"
    # (0.90 - 2.00) x 100 — a loss, bounded by the $200 premium.
    assert summary["realized_pnl_usd"] == -110.0
    assert summary["overlay_equity_usd"] == 390.0


def test_flatten_at_1555_sells_the_contract():
    broker = SimulatedOptionsBroker()
    broker.set_chain(standard_chain())
    symbol = occ("SPY", DAY, "C", 101)
    broker.set_quote(symbol, fresh_quote(ask=1.80))
    runner, events, _ = make_options_runner(broker)
    bars = range_bars() + [mkbar(DAY, 9, 45, 99.8, 100.7, 99.7, 100.6)]
    t = datetime(2026, 10, 21, 9, 46, tzinfo=ET)
    while t.time() < time(15, 55):
        bars.append(mkbar(DAY, t.hour, t.minute, 101.0, 101.3, 100.7, 101.0))
        t += timedelta(minutes=1)
    bars.append(mkbar(DAY, 15, 55, 101.0, 101.2, 100.8, 101.1))
    summary = run_options(
        runner, broker, bars,
        lambda bar: 2.30 if bar.ts.time() >= time(15, 55) else 1.80,
    )
    assert summary["trades"] == 1
    (closed,) = [e for e in events if e["type"] == "trade_closed"]
    assert closed["exit_reason"] == "EOD_FLATTEN"
    assert summary["realized_pnl_usd"] == 50.0
    assert broker.position_qty(symbol) == 0


def test_premium_gate_skips_expensive_day():
    broker = SimulatedOptionsBroker()
    broker.set_chain(standard_chain())
    symbol = occ("SPY", DAY, "C", 101)
    broker.set_quote(symbol, fresh_quote(ask=2.75))  # > $2.50 cap
    runner, events, _ = make_options_runner(broker)
    summary = run_options(runner, broker, long_target_day(), lambda bar: 2.75)
    assert broker.orders == []
    assert summary["trades"] == 0
    assert summary["premium_skips"] == 1
    assert summary["no_trade_reason"] == "premium_skip"
    (skip,) = [e for e in events if e["type"] == "premium_skip"]
    assert skip["reason"] == "ask_above_cap"
    assert skip["ask"] == 2.75
    # The strategy's simulated trade still resolved, observed only.
    assert "strategy_trade_closed_observe_only" in event_types(events)


def test_premium_gate_at_cap_boundary_enters():
    broker = SimulatedOptionsBroker()
    broker.set_chain(standard_chain())
    symbol = occ("SPY", DAY, "C", 101)
    broker.set_quote(symbol, fresh_quote(ask=2.50))  # exactly at cap
    runner, events, _ = make_options_runner(broker)
    summary = run_options(
        runner, broker, long_target_day(),
        lambda bar: 3.00 if bar.ts.time() >= time(9, 47) else 2.50,
    )
    assert summary["premium_skips"] == 0
    assert summary["trades"] == 1
    assert summary["premium_paid_usd"] == 250.0  # the cap IS the max loss


def test_stale_snapshot_skips_the_day():
    broker = SimulatedOptionsBroker()
    broker.set_chain(standard_chain())
    symbol = occ("SPY", DAY, "C", 101)
    stale_ts = datetime(2026, 10, 21, 13, 0, tzinfo=UTC)  # 46 min old
    broker.set_quote(symbol, fresh_quote(ask=1.80, ts=stale_ts))
    runner, events, _ = make_options_runner(broker)
    summary = run_options(runner, broker, long_target_day(), lambda bar: 1.80)
    assert broker.orders == []
    assert summary["premium_skips"] == 1
    (skip,) = [e for e in events if e["type"] == "premium_skip"]
    assert skip["reason"] == "stale_quote"


def test_missing_snapshot_skips_the_day():
    broker = SimulatedOptionsBroker()
    broker.set_chain(standard_chain())
    runner, events, _ = make_options_runner(broker)  # no quote set at all
    summary = run_options(runner, broker, long_target_day(), lambda bar: 1.80)
    assert broker.orders == []
    assert summary["premium_skips"] == 1
    (skip,) = [e for e in events if e["type"] == "premium_skip"]
    assert skip["reason"] == "quote_unavailable"


def test_no_contract_available_skips_the_day():
    broker = SimulatedOptionsBroker()
    broker.set_chain([])  # empty chain
    runner, events, _ = make_options_runner(broker)
    summary = run_options(runner, broker, long_target_day(), lambda bar: 1.80)
    assert broker.orders == []
    assert summary["trades"] == 0
    assert summary["no_trade_reason"] == "contract_unavailable"
    assert "contract_unavailable" in event_types(events)


def test_kill_switch_blocks_option_entry():
    broker = SimulatedOptionsBroker()
    broker.set_chain(standard_chain())
    symbol = occ("SPY", DAY, "C", 101)
    broker.set_quote(symbol, fresh_quote(ask=1.80))
    runner, events, _ = make_options_runner(broker, enabled=False)
    summary = run_options(runner, broker, long_target_day(), lambda bar: 1.80)
    assert broker.orders == []
    assert summary["orders_blocked_kill_switch"] == 1
    assert summary["trades"] == 0
    assert "order_blocked_kill_switch" in event_types(events)


def test_frontier_catchup_signal_is_a_missed_day():
    broker = SimulatedOptionsBroker()
    broker.set_chain(standard_chain())
    symbol = occ("SPY", DAY, "C", 101)
    broker.set_quote(symbol, fresh_quote(ask=1.80))
    runner, events, store = make_options_runner(broker)
    summary = runner.run_day(
        DAY,
        priced_option_bars(long_target_day(), broker, lambda bar: 1.80),
        live_since=datetime(2026, 10, 21, 14, 32, tzinfo=ET),
    )
    assert broker.orders == []
    assert summary["missed_day"] is True
    assert summary["trades"] == 0
    assert summary["overlay_equity_usd"] == 500.0
    (missed_ev,) = [e for e in events if e["type"] == "missed_day"]
    assert missed_ev["outcome"] == "closed_in_replay"
    assert missed_ev["booked"] is False


# ----------------------------------------------------------------------
# restart / reconciliation: adoption, leftovers, orphans
# ----------------------------------------------------------------------
def _options_state(day=DAY, **position_overrides):
    position = {
        "contract_symbol": occ("SPY", DAY, "C", 101),
        "option_type": "call", "direction": "long",
        "expiry": DAY.isoformat(), "strike": 101.0, "qty_contracts": 1,
        "entry_premium_usd": 1.80, "underlying_entry_price": 100.6,
        "stop": 99.0, "target": 103.8, "entry_time": "09:45",
        "entry_order_id": "sim-opt-seed",
    }
    position.update(position_overrides)
    return {
        "version": 1, "variant": "orb15_options",
        "date": day.isoformat(), "phase": "IN_POSITION",
        "position": position, "overlay_equity_usd": 500.0,
        "realized_pnl_total_usd": 0.0, "halted": False,
        "halt_reason": None,
    }


def test_restart_adopts_contract_and_exits_on_target():
    symbol = occ("SPY", DAY, "C", 101)
    broker = SimulatedOptionsBroker()
    broker.seed_position(symbol, 1)
    store = StateStore()
    store.save(_options_state())
    runner, events, _ = make_options_runner(broker, store=store)
    summary = run_options(
        runner, broker, long_target_day(),
        lambda bar: 3.25 if bar.ts.time() >= time(9, 47) else 1.80,
    )
    assert summary["halted"] is False
    assert summary["trades"] == 1
    # Only the exit order — the entry was adopted, not re-bought.
    assert [(o["side"], o["qty"]) for o in broker.orders] == [("sell", 1)]
    assert "entry_adopted_after_restart" in event_types(events)
    assert summary["overlay_equity_usd"] == 645.0


def test_orphan_option_position_is_flattened_and_day_halts():
    orphan_symbol = occ("SPY", TOMORROW, "P", 99)
    broker = SimulatedOptionsBroker()
    broker.set_premium(1.10)
    broker.seed_position(orphan_symbol, 1)  # state knows nothing of it
    runner, events, store = make_options_runner(broker)
    summary = run_options(runner, broker, long_target_day(), lambda bar: 1.10)
    assert summary["halted"] is True
    assert "orphan" in summary["halt_reason"]
    # Flattened via close_position, never left riding; no entries.
    assert broker.position_qty(orphan_symbol) == 0
    assert [(o["kind"], o["symbol"]) for o in broker.orders] == [
        ("close_position", orphan_symbol)
    ]
    (orphan_ev,) = [e for e in events if e["type"] == "orphan_flattened"]
    assert orphan_ev["contract_symbol"] == orphan_symbol
    assert orphan_ev["proceeds_usd"] == 110.0
    assert orphan_ev["booked_pnl_usd"] is None
    assert store.load()["halted"] is True
    # And the halt is sticky the next day too.
    runner2, events2, _ = make_options_runner(broker, store=store)
    summary2 = runner2.run_day(
        TOMORROW, priced_option_bars([], broker, lambda bar: 1.0)
    )
    assert summary2["halted"] is True
    assert "runner_start_halted" in event_types(events2)


def test_overnight_leftover_contract_flattened_booked_and_day_continues():
    leftover_symbol = occ("SPY", YESTERDAY, "C", 101)
    broker = SimulatedOptionsBroker()
    broker.set_premium(2.60)  # the leftover close fills here
    broker.seed_position(leftover_symbol, 1)
    broker.set_chain(standard_chain())
    call_symbol = occ("SPY", DAY, "C", 101)
    broker.set_quote(call_symbol, fresh_quote(ask=1.80))
    store = StateStore()
    store.save(_options_state(
        day=YESTERDAY,
        contract_symbol=leftover_symbol,
        expiry=YESTERDAY.isoformat(),
        entry_premium_usd=2.00,
    ))
    runner, events, _ = make_options_runner(broker, store=store)
    summary = run_options(
        runner, broker, long_target_day(),
        lambda bar: 3.25 if bar.ts.time() >= time(9, 47) else 1.80,
    )
    assert summary["halted"] is False
    types = event_types(events)
    assert "overnight_leftover_detected" in types
    (left_ev,) = [e for e in events if e["type"] == "overnight_leftover_flattened"]
    assert left_ev["pnl_usd"] == 60.0  # (2.60 - 2.00) x 100
    # Then the day traded normally: +145 more on top of the +60.
    assert summary["trades"] == 1
    assert summary["overlay_equity_usd"] == 705.0
    assert broker.position_qty(leftover_symbol) == 0
    assert broker.position_qty(call_symbol) == 0


def test_state_position_missing_at_broker_halts():
    broker = SimulatedOptionsBroker()  # flat; state claims a contract
    store = StateStore()
    store.save(_options_state())
    runner, events, _ = make_options_runner(broker, store=store)
    summary = run_options(runner, broker, long_target_day(), lambda bar: 1.80)
    assert summary["halted"] is True
    assert "vanished" in summary["halt_reason"]
    assert broker.orders == []


# ----------------------------------------------------------------------
# exit failure handling: a rejected exit order falls back to a
# whole-position close instead of killing the day (audit F3 pattern)
# ----------------------------------------------------------------------
class FlakyExitBroker(SimulatedOptionsBroker):
    def place_market(self, symbol, qty, side):
        if side == "sell":
            from alpaca.runner import OrderError

            raise OrderError("simulated exit-order rejection")
        return super().place_market(symbol, qty, side)


def test_exit_order_rejection_falls_back_to_close_position():
    broker = FlakyExitBroker()
    broker.set_chain(standard_chain())
    symbol = occ("SPY", DAY, "C", 101)
    broker.set_quote(symbol, fresh_quote(ask=1.80))
    runner, events, _ = make_options_runner(broker)
    summary = run_options(
        runner, broker, long_target_day(),
        lambda bar: 3.25 if bar.ts.time() >= time(9, 47) else 1.80,
    )
    assert summary["trades"] == 1
    assert summary["realized_pnl_usd"] == 145.0
    assert broker.position_qty(symbol) == 0
    types = event_types(events)
    assert "exit_order_failed" in types
    assert "exit_failure_critical" not in types
    assert [o["kind"] for o in broker.orders] == ["order", "close_position"]
    (closed,) = [e for e in events if e["type"] == "trade_closed"]
    assert closed["exit_reason"] == "TARGET"
