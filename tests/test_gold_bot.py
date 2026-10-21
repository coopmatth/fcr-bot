"""Mocked tests for the FCR gold bot runner (gold_bot.py).

Run from the project root:  python3 -m pytest tests/ -q

NO real network calls and NO real credentials: the Tradovate client
runs on a FakeTransport, the market-data socket is a FakeSocket, and
all credential values are obvious fakes. These tests prove the runner
wires bars -> engine -> demo bracket order correctly, flattens at
15:55 ET, and refuses to run with the kill switch off-position, on a
non-demo host, or with a strategy config that drifts from the frozen
validated gold spec (results/gold24.json, V0 baseline).
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import gold_bot
from fcr.config import load_config
from fcr.patterns import Bar
from tradovate.client import TradovateClient
from tradovate.config import (
    LIVE_BASE_URL,
    LiveTradingDisabledError,
    TradovateConfig,
)
from tradovate.execution import ExecutionAdapter
from tradovate.market_data import MarketDataAdapter

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
DAY = "2026-10-20"
TODAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

GOLD_CFG = load_config(ROOT / "config_gold.yaml")

AUTH_OK = {
    "accessToken": "fake-access-token",
    "mdAccessToken": "fake-md-token",
    "expirationTime": "2026-10-20T13:00:00.000Z",
    "userId": 999,
}
SUGGEST_MGC = [
    {"id": 555, "name": "MGCV6", "contractMaturityId": 1},
    {"id": 556, "name": "MGCZ6", "contractMaturityId": 2},
    {"id": 557, "name": "MGCG7", "contractMaturityId": 3},
]
ACCOUNT = [{"id": 42, "name": "fake-demo-user"}]
OPEN_POSITION = [{"id": 9, "accountId": 42, "contractId": 555, "netPos": 5}]


class FakeTransport:
    """Same contract as tests/test_tradovate.py: routes by URL suffix,
    records every call, unrouted calls are assertion failures."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        parsed = json.loads(body.decode()) if body else None
        call = {"method": method, "url": url, "headers": dict(headers), "body": parsed}
        self.calls.append(call)
        for suffix, response in self.routes.items():
            if url.split("?")[0].endswith(suffix):
                status, payload = response(call) if callable(response) else response
                return status, {}, json.dumps(payload).encode()
        raise AssertionError(f"Unexpected transport call: {method} {url}")

    def calls_to(self, suffix):
        return [c for c in self.calls if c["url"].split("?")[0].endswith(suffix)]


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def recv(self):
        if not self.messages:
            raise StopIteration
        return self.messages.pop(0)


def make_runner(routes, **runner_kwargs):
    transport = FakeTransport(routes)
    config = TradovateConfig(
        username="fake-demo-user", password="fake-password",
        api_key="fake-api-key", api_secret="fake-api-secret", cid=12345,
    )
    client = TradovateClient(config, transport=transport, clock=lambda: NOW)
    runner = gold_bot.GoldBotRunner(
        GOLD_CFG,
        MarketDataAdapter(client),
        ExecutionAdapter(client, account_spec="fake-demo-user"),
        **runner_kwargs,
    )
    return runner, transport


def base_routes(position):
    return {
        "/auth/accesstokenrequest": (200, AUTH_OK),
        "/contract/suggest": (200, SUGGEST_MGC),
        "/account/list": (200, ACCOUNT),
        "/order/placeOSO": (200, {"orderId": 1, "oso1Id": 2, "oso2Id": 3}),
        "/position/list": (200, position),
        "/order/liquidatePosition": (200, {"orderId": 55}),
    }


def bar(hhmm, o, h, l, c):
    hh, mm = map(int, hhmm.split(":"))
    return Bar(ts=datetime(2026, 10, 20, hh, mm, tzinfo=ET),
               open=o, high=h, low=l, close=c)


# First candle 09:30-09:34: high 101.0, low 99.0 (same geometry as the
# engine scenario tests). Bullish FVG [100.5, 101.2], retest, engulfing
# entry at 09:40 close 101.5, stop 100.5, target 104.5. On MGC ($10/pt)
# a 1.0-pt stop risks $10/contract -> floor(150/10) = 15, capped at 5.
LONG_DAY = [
    bar("09:30", 100.0, 100.6, 99.4, 100.2),
    bar("09:31", 100.2, 101.0, 99.8, 100.5),
    bar("09:32", 100.5, 100.8, 99.0, 100.1),
    bar("09:33", 100.1, 100.7, 99.6, 100.4),
    bar("09:34", 100.4, 100.9, 100.0, 100.3),
    bar("09:35", 100.2, 100.5, 99.9, 100.3),
    bar("09:36", 100.3, 101.9, 100.3, 101.7),
    bar("09:37", 101.7, 102.0, 101.2, 101.8),
    bar("09:38", 101.8, 101.9, 101.3, 101.4),
    bar("09:39", 101.4, 101.45, 100.9, 101.0),
    bar("09:40", 100.95, 101.6, 100.9, 101.5),   # ENTRY (long, 5 contracts)
]
FLATTEN_BAR = bar("15:55", 102.0, 102.1, 101.9, 102.0)
QUIET_DAY = LONG_DAY[:5] + [
    bar("09:35", 100.2, 100.6, 99.8, 100.3),     # never breaks a level
    bar("09:36", 100.3, 100.7, 99.9, 100.4),
    FLATTEN_BAR,
]


# ------------------------------------------------------------- happy path
def test_long_setup_places_one_bracket_and_flattens():
    runner, transport = make_runner(base_routes(OPEN_POSITION))
    result = runner.run_day(DAY, LONG_DAY + [FLATTEN_BAR], today=TODAY)

    # Contract + account resolved through the adapters (front month
    # MGCV6 on 2026-10-20; account auto-resolved from /account/list).
    assert result.contract_name == "MGCV6"
    assert result.contract_id == 555
    assert result.account_id == 42
    assert transport.calls_to("/contract/suggest")
    assert transport.calls_to("/account/list")

    # Exactly one bracket entry, carrying the engine's own geometry.
    assert result.entered is True
    oso = transport.calls_to("/order/placeOSO")
    assert len(oso) == 1
    payload = oso[0]["body"]
    assert payload["accountId"] == 42
    assert payload["accountSpec"] == "fake-demo-user"
    assert payload["symbol"] == "MGCV6"
    assert payload["action"] == "Buy"
    assert payload["orderQty"] == 5
    assert payload["orderType"] == "Market"
    assert payload["bracket1"] == {"action": "Sell", "orderType": "Limit", "price": 104.5}
    assert payload["bracket2"] == {"action": "Sell", "orderType": "Stop", "stopPrice": 100.5}
    assert oso[0]["url"].startswith("https://demo.tradovateapi.com/v1")

    # 15:55: the engine flattened its sim at the bar's open (+$25.00)
    # and the runner liquidated the still-open demo position once.
    assert result.flattened is True
    liquidations = transport.calls_to("/order/liquidatePosition")
    assert len(liquidations) == 1
    assert liquidations[0]["body"] == {
        "accountId": 42, "contractId": 555, "isAutomated": True,
    }
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade["exit_reason"] == "EOD_FLATTEN"
    assert trade["pnl_usd"] == 25.0


def test_target_hit_closes_in_sim_and_flatten_sends_nothing():
    runner, transport = make_runner(base_routes([]))
    to_target = bar("09:41", 101.5, 104.6, 101.4, 104.4)  # high reaches 104.5
    result = runner.run_day(DAY, LONG_DAY + [to_target, FLATTEN_BAR], today=TODAY)
    assert len(transport.calls_to("/order/placeOSO")) == 1
    assert len(result.trades) == 1
    assert result.trades[0]["exit_reason"] == "TARGET"
    assert result.trades[0]["pnl_usd"] == 150.0  # +3R x 1.0pt x 5 x $10
    # The bracket already closed the demo position: flatten finds none.
    assert result.flattened is True
    assert transport.calls_to("/order/liquidatePosition") == []


def test_quiet_day_places_no_orders():
    runner, transport = make_runner(base_routes([]))
    result = runner.run_day(DAY, QUIET_DAY, today=TODAY)
    assert result.entered is False
    assert result.trades == []
    assert transport.calls_to("/order/placeOSO") == []
    assert transport.calls_to("/order/liquidatePosition") == []


def test_stream_day_wires_socket_bars_to_the_same_entry():
    runner, transport = make_runner(base_routes(OPEN_POSITION))

    def chart_frame(bars):
        raw = []
        for b in bars:
            ts = b.ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            raw.append({"timestamp": ts, "open": b.open, "high": b.high,
                        "low": b.low, "close": b.close,
                        "upVolume": 10, "downVolume": 5})
        return "a" + json.dumps(
            [{"e": "chart", "d": {"charts": [{"id": 1, "td": 20261020, "bars": raw}]}}]
        )

    socket = FakeSocket(["o", chart_frame(LONG_DAY), chart_frame([FLATTEN_BAR])])
    result = runner.run_stream_day(DAY, socket, today=TODAY)
    assert result.entered is True
    assert result.contract_name == "MGCV6"
    assert len(transport.calls_to("/order/placeOSO")) == 1
    assert len(transport.calls_to("/order/liquidatePosition")) == 1
    # The socket handshake used the md token from the same auth.
    assert socket.sent[0] == "authorize\n0\n\nfake-md-token"
    assert json.loads(socket.sent[1].split("\n", 3)[3])["symbol"] == "MGCV6"


# ------------------------------------------------------------ bar dedupe
def test_dedupe_closed_bars_keeps_last_update_per_timestamp():
    b1a = bar("09:30", 100.0, 100.5, 99.5, 100.1)
    b1b = bar("09:30", 100.0, 100.9, 99.5, 100.4)  # same minute, later update
    b2 = bar("09:31", 100.4, 100.6, 100.2, 100.5)
    out = list(gold_bot.dedupe_closed_bars([b1a, b1b, b2]))
    assert out == [b1b, b2]


# ---------------------------------------------------------------- safety
def test_runner_refuses_when_kill_switch_flipped(monkeypatch):
    monkeypatch.setattr(gold_bot, "LIVE_TRADING_ENABLED", True)
    with pytest.raises(LiveTradingDisabledError):
        make_runner(base_routes([]))


def test_runner_refuses_non_demo_client():
    class FakeLiveConfig:
        base_url = LIVE_BASE_URL

    class FakeLiveClient:
        config = FakeLiveConfig()

    class StubExecution:
        client = FakeLiveClient()

    with pytest.raises(LiveTradingDisabledError):
        gold_bot.GoldBotRunner(GOLD_CFG, MarketDataAdapter(None), StubExecution())


def test_runner_refuses_config_drift_from_frozen_spec():
    for drift in (
        {"symbol": "MNQ"},
        {"point_value_usd": 2.0},
        {"risk_budget_usd": 100},
        {"max_contracts": 3},
        {"rr_target": 2.0},
        {"partials": True},          # V3 flag: not the validated winner
        {"trend_filter": True},      # V4 flag
        {"min_fvg_atr_mult": 0.5},   # V1 flag
    ):
        cfg = dict(GOLD_CFG)
        cfg.update(drift)
        with pytest.raises(ValueError, match="frozen validated"):
            gold_bot.validate_strategy_config(cfg)
    # The shipped config_gold.yaml itself passes untouched.
    assert gold_bot.validate_strategy_config(dict(GOLD_CFG))["symbol"] == "MGC"


def test_dry_run_records_without_any_transport():
    recording = gold_bot.RecordingExecution()
    runner = gold_bot.GoldBotRunner(
        GOLD_CFG,
        market_data=None,
        execution=recording,
        contract_override={"id": 0, "name": "MGC-DRYRUN"},
    )
    result = runner.run_day(DAY, LONG_DAY + [FLATTEN_BAR], today=TODAY)
    assert result.entered is True
    assert len(recording.entries) == 1
    assert recording.entries[0]["symbol"] == "MGC-DRYRUN"
    assert recording.entries[0]["orderQty"] == 5
    assert recording.flatten_calls == [(0, 0)]
