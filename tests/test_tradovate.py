"""Mocked tests for the Tradovate DEMO-only integration.

Run from the project root:  python3 -m pytest tests/ -q

NO real network calls: every client is built on FakeTransport and
every socket is a FakeSocket. No real credentials exist for this
project; the values below are obvious fakes.

Endpoint shapes asserted here are the documented ones — see the
docstrings in tradovate/*.py for the doc URL behind each.
"""
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from fcr.patterns import Bar
from tradovate import config as tv_config
from tradovate.client import TradovateAPIError, TradovateClient
from tradovate.config import (
    DEMO_BASE_URL,
    LIVE_BASE_URL,
    LIVE_TRADING_ENABLED,
    MARKET_DATA_WS_URL,
    LiveTradingDisabledError,
    MissingCredentialsError,
    TradovateConfig,
)
from tradovate.execution import ExecutionAdapter, build_bracket_entry_payload
from tradovate.market_data import (
    HEARTBEAT_FRAME,
    MarketDataAdapter,
    bars_from_frame,
    build_get_chart_body,
    format_authorize,
    format_ws_request,
    parse_bar,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)  # 09:00 ET

FAKE_ENV = {
    "TRADOVATE_DEMO_USERNAME": "fake-demo-user",
    "TRADOVATE_DEMO_PASSWORD": "fake-password",
    "TRADOVATE_DEMO_API_KEY": "fake-api-key",
    "TRADOVATE_DEMO_API_SECRET": "fake-api-secret",
    "TRADOVATE_DEMO_CID": "12345",
}

AUTH_OK = {
    "accessToken": "fake-access-token",
    "mdAccessToken": "fake-md-token",
    "expirationTime": "2026-10-21T14:30:00.000Z",
    "userStatus": "Active",
    "userId": 999,
    "name": "fake-demo-user",
    "hasLive": False,
    "hasMarketData": True,
}


def fixed_clock():
    return NOW


class FakeTransport:
    """Records calls; routes by URL suffix to a canned response.

    routes: {url_suffix: (status, payload) or callable(call)->(status, payload)}
    Any unrouted call is an assertion failure — a test must declare
    every request the code is expected to make."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        parsed_body = json.loads(body.decode()) if body else None
        call = {"method": method, "url": url, "headers": dict(headers), "body": parsed_body}
        self.calls.append(call)
        for suffix, response in self.routes.items():
            if url.split("?")[0].endswith(suffix):
                status, payload = response(call) if callable(response) else response
                return status, {}, json.dumps(payload).encode()
        raise AssertionError(f"Unexpected transport call: {method} {url}")

    def calls_to(self, suffix):
        return [c for c in self.calls if c["url"].split("?")[0].endswith(suffix)]


class FakeSocket:
    """send() records; recv() replays queued raw frames, then stops."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def recv(self):
        if not self.messages:
            raise StopIteration
        return self.messages.pop(0)


def make_config(**overrides):
    kwargs = dict(
        username="fake-demo-user",
        password="fake-password",
        api_key="fake-api-key",
        api_secret="fake-api-secret",
        cid=12345,
    )
    kwargs.update(overrides)
    return TradovateConfig(**kwargs)


def make_client(routes, **kwargs):
    transport = FakeTransport(routes)
    client = TradovateClient(
        make_config(), transport=transport, clock=fixed_clock, **kwargs
    )
    return client, transport


def authed_client(extra_routes):
    routes = {"/auth/accesstokenrequest": (200, AUTH_OK)}
    routes.update(extra_routes)
    return make_client(routes)


# ------------------------------------------------------------------ config
def test_config_from_env_and_demo_urls():
    cfg = TradovateConfig.from_env(FAKE_ENV)
    assert cfg.username == "fake-demo-user"
    assert cfg.cid == 12345 and isinstance(cfg.cid, int)
    assert cfg.base_url == DEMO_BASE_URL == "https://demo.tradovateapi.com/v1"
    assert cfg.market_data_ws_url == MARKET_DATA_WS_URL
    assert cfg.market_data_ws_url == "wss://md.tradovateapi.com/v1/websocket"
    # The live constant exists but is never selected while the switch is off.
    assert LIVE_BASE_URL == "https://live.tradovateapi.com/v1"
    assert LIVE_TRADING_ENABLED is False
    assert cfg.base_url != LIVE_BASE_URL


def test_missing_credentials_error_names_every_env_var():
    with pytest.raises(MissingCredentialsError) as excinfo:
        TradovateConfig.from_env({})
    message = str(excinfo.value)
    for name in (
        "TRADOVATE_DEMO_USERNAME",
        "TRADOVATE_DEMO_PASSWORD",
        "TRADOVATE_DEMO_API_KEY",
        "TRADOVATE_DEMO_API_SECRET",
        "TRADOVATE_DEMO_CID",
    ):
        assert name in message


def test_missing_credentials_lists_only_the_missing_ones():
    partial = dict(FAKE_ENV)
    del partial["TRADOVATE_DEMO_API_SECRET"]
    partial["TRADOVATE_DEMO_CID"] = "  "
    with pytest.raises(MissingCredentialsError) as excinfo:
        TradovateConfig.from_env(partial)
    message = str(excinfo.value)
    assert "TRADOVATE_DEMO_API_SECRET" in message
    assert "TRADOVATE_DEMO_CID" in message
    assert "TRADOVATE_DEMO_USERNAME" not in message
    # No credential value ever appears in the error.
    assert "fake-password" not in message


def test_non_integer_cid_is_a_clear_error():
    bad = dict(FAKE_ENV, TRADOVATE_DEMO_CID="not-a-number")
    with pytest.raises(MissingCredentialsError, match="TRADOVATE_DEMO_CID"):
        TradovateConfig.from_env(bad)


# ------------------------------------------------------------- kill switch
def test_kill_switch_config_refuses_live_environment():
    with pytest.raises(LiveTradingDisabledError):
        make_config(environment="live")


def test_kill_switch_execution_adapter_refuses_non_demo():
    client, _transport = authed_client({})
    for env in ("live", "production", "replay", "LIVE", ""):
        with pytest.raises(LiveTradingDisabledError):
            ExecutionAdapter(client, environment=env)


def test_kill_switch_execution_adapter_refuses_live_based_client():
    class FakeLiveConfig:
        base_url = LIVE_BASE_URL

    class FakeLiveClient:
        config = FakeLiveConfig()

    with pytest.raises(LiveTradingDisabledError):
        ExecutionAdapter(FakeLiveClient(), environment="demo")


# -------------------------------------------------------------------- auth
def test_auth_success_token_cached_and_expiry_parsed():
    client, transport = make_client({"/auth/accesstokenrequest": (200, AUTH_OK)})
    token = client.get_access_token()
    assert token == "fake-access-token"
    info = client.token_info
    assert info.md_access_token == "fake-md-token"
    assert info.user_id == 999
    # expirationTime "2026-10-21T14:30:00.000Z" parsed to aware UTC.
    assert info.expiration_time == datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)
    assert info.expiration_time.tzinfo is not None
    # Second call is served from cache: still exactly one HTTP call.
    assert client.get_access_token() == "fake-access-token"
    assert len(transport.calls) == 1
    # The request itself matches the documented shape.
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"
    assert call["body"]["name"] == "fake-demo-user"
    assert call["body"]["password"] == "fake-password"
    assert call["body"]["cid"] == 12345
    assert call["body"]["sec"] == "fake-api-secret"
    assert call["body"]["appId"] == "fake-api-key"
    assert call["body"]["appVersion"] == "1.0"


def test_auth_failure_http_error_surfaces():
    client, _ = make_client(
        {"/auth/accesstokenrequest": (401, {"errorText": "Incorrect username or password"})}
    )
    with pytest.raises(TradovateAPIError, match="Incorrect username or password"):
        client.get_access_token()


def test_auth_failure_error_text_body_surfaces_even_on_200():
    client, _ = make_client(
        {"/auth/accesstokenrequest": (200, {"errorText": "Access is denied"})}
    )
    with pytest.raises(TradovateAPIError, match="Access is denied"):
        client.get_access_token()


def test_renew_access_token_uses_documented_endpoint():
    renewed = dict(AUTH_OK, accessToken="renewed-token",
                   expirationTime="2026-10-21T16:00:00.000Z")
    client, transport = make_client(
        {
            "/auth/accesstokenrequest": (200, AUTH_OK),
            "/auth/renewaccesstoken": (200, renewed),
        }
    )
    client.request_access_token()
    info = client.renew_access_token()
    assert info.access_token == "renewed-token"
    renew_call = transport.calls_to("/auth/renewaccesstoken")[0]
    assert renew_call["method"] == "GET"
    assert renew_call["headers"]["Authorization"] == "Bearer fake-access-token"


def test_authorized_request_attaches_bearer_and_hits_demo_only():
    client, transport = authed_client({"/account/list": (200, [{"id": 7}])})
    assert client.get("/account/list") == [{"id": 7}]
    call = transport.calls_to("/account/list")[0]
    assert call["headers"]["Authorization"] == "Bearer fake-access-token"
    assert all(c["url"].startswith(DEMO_BASE_URL) for c in transport.calls)


# ------------------------------------------------------------- bar parsing
# Sample bar payload: the documented md/getChart data message
# (docs.ninjatrader.com/market-data), wrapped in a SockJS 'a' frame
# exactly as the WebSocket docs specify.
CHART_FRAME = "a" + json.dumps(
    [
        {
            "e": "chart",
            "d": {
                "charts": [
                    {
                        "id": 31,
                        "td": 20170413,
                        "bars": [
                            {
                                "timestamp": "2017-04-13T11:00:00.000Z",
                                "open": 2334.25,
                                "high": 2334.5,
                                "low": 2333,
                                "close": 2333.75,
                                "upVolume": 4712,
                                "downVolume": 201,
                                "upTicks": 1334,
                                "downTicks": 83,
                                "bidVolume": 2857,
                                "offerVolume": 2056,
                            },
                            {
                                "timestamp": "2017-04-13T11:01:00.000Z",
                                "open": 2333.75,
                                "high": 2335.0,
                                "low": 2333.5,
                                "close": 2334.75,
                                "upVolume": 100,
                                "downVolume": 50,
                            },
                        ],
                    }
                ]
            },
        }
    ]
)


def test_bar_parsing_from_documented_chart_payload():
    bars = bars_from_frame(CHART_FRAME)
    assert len(bars) == 2
    first = bars[0]
    assert isinstance(first, Bar)
    assert first.open == 2334.25
    assert first.high == 2334.5
    assert first.low == 2333.0
    assert first.close == 2333.75
    assert first.volume == 4712 + 201  # upVolume + downVolume
    # 11:00 UTC in April is 07:00 in America/New_York (EDT, UTC-4).
    assert first.ts == datetime(2017, 4, 13, 7, 0, tzinfo=ET)
    assert first.ts.tzinfo is not None
    assert bars[1].ts == datetime(2017, 4, 13, 7, 1, tzinfo=ET)


def test_parse_bar_direct_dict_and_non_chart_frames():
    bar = parse_bar(
        {
            "timestamp": "2026-10-21T13:30:00.000Z",
            "open": 25000.0,
            "high": 25010.0,
            "low": 24990.0,
            "close": 25005.0,
        }
    )
    assert bar.ts == datetime(2026, 10, 21, 9, 30, tzinfo=ET)
    assert bar.volume == 0.0
    assert bars_from_frame("h") == []  # heartbeat frame: no bars
    assert bars_from_frame('a[{"e":"md","d":{"quotes":[]}}]') == []


def test_get_chart_request_shape_and_ws_framing():
    body = build_get_chart_body("MNQZ6", as_much_as_elements=50)
    assert body["symbol"] == "MNQZ6"
    assert body["chartDescription"] == {
        "underlyingType": "MinuteBar",
        "elementSize": 1,
        "elementSizeUnit": "UnderlyingUnits",
        "withHistogram": False,
    }
    assert body["timeRange"] == {"asMuchAsElements": 50}
    # Client request frame: endpoint\nid\n\n<json body>
    frame = format_ws_request("md/getChart", 3, body=body)
    head, req_id, blank, payload = frame.split("\n", 3)
    assert (head, req_id, blank) == ("md/getChart", "3", "")
    assert json.loads(payload) == body
    # Authorize frame carries the token as its body.
    assert format_authorize(0, "tok") == "authorize\n0\n\ntok"
    assert HEARTBEAT_FRAME == "[]"


def test_stream_bars_with_fake_socket():
    socket = FakeSocket(["o", CHART_FRAME, "h"])
    client, _ = authed_client({})
    adapter = MarketDataAdapter(client)
    bars = list(
        adapter.stream_bars(socket, "MNQZ6", "fake-md-token", max_messages=3)
    )
    assert len(bars) == 2
    assert bars[0].close == 2333.75
    # Handshake: authorize first, then the md/getChart request.
    assert socket.sent[0] == "authorize\n0\n\nfake-md-token"
    assert socket.sent[1].startswith("md/getChart\n1\n\n")
    assert json.loads(socket.sent[1].split("\n", 3)[3])["symbol"] == "MNQZ6"
    # The server heartbeat frame was answered with the client heartbeat.
    assert socket.sent[-1] == "[]"


# --------------------------------------------------------------- contracts
SUGGEST_MNQ = [
    {"id": 111, "name": "MNQU6", "contractMaturityId": 1},
    {"id": 222, "name": "MNQZ6", "contractMaturityId": 2},
    {"id": 333, "name": "MNQH7", "contractMaturityId": 3},
]


def test_contract_find_and_front_month_resolution():
    client, transport = authed_client(
        {
            "/contract/suggest": (200, SUGGEST_MNQ),
            "/contract/find": (200, {"id": 222, "name": "MNQZ6", "contractMaturityId": 2}),
        }
    )
    adapter = MarketDataAdapter(client)
    found = adapter.find_contract("MNQZ6")
    assert found["id"] == 222
    find_call = transport.calls_to("/contract/find")[0]
    assert "name=MNQZ6" in find_call["url"]
    # On 2026-10-21 the front month is Dec 2026 (MNQZ6): Sep 2026 is
    # past, Mar 2027 is later.
    front = adapter.resolve_front_month("MNQ", today=date(2026, 10, 21))
    assert front["name"] == "MNQZ6"
    assert front["id"] == 222
    suggest_call = transport.calls_to("/contract/suggest")[0]
    assert "t=MNQ" in suggest_call["url"]


# ---------------------------------------------------------------- execution
def make_adapter(extra_routes, account_spec="fake-demo-user"):
    client, transport = authed_client(extra_routes)
    return ExecutionAdapter(client, account_spec=account_spec), transport


def test_bracket_order_payload_shape_long():
    adapter, transport = make_adapter(
        {"/order/placeOSO": (200, {"orderId": 1, "oso1Id": 2, "oso2Id": 3})}
    )
    result = adapter.place_bracket_entry(
        account_id=42,
        contract_id="MNQZ6",
        direction="long",
        contracts=2,
        stop_price=24900.0,
        target_price=25300.0,
    )
    assert result == {"orderId": 1, "oso1Id": 2, "oso2Id": 3}
    call = transport.calls_to("/order/placeOSO")[0]
    assert call["method"] == "POST"
    assert call["url"].startswith(DEMO_BASE_URL)
    payload = call["body"]
    assert payload["accountId"] == 42
    assert payload["accountSpec"] == "fake-demo-user"
    assert payload["symbol"] == "MNQZ6"
    assert payload["action"] == "Buy"
    assert payload["orderQty"] == 2
    assert payload["orderType"] == "Market"
    assert payload["isAutomated"] is True
    # Take-profit leg: opposite action, Limit at the target price.
    assert payload["bracket1"] == {
        "action": "Sell",
        "orderType": "Limit",
        "price": 25300.0,
    }
    # Stop-loss leg: opposite action, Stop at the stop price.
    assert payload["bracket2"] == {
        "action": "Sell",
        "orderType": "Stop",
        "stopPrice": 24900.0,
    }


def test_bracket_order_payload_shape_short_and_int_contract_id():
    payload = build_bracket_entry_payload(
        account_id=42,
        contract_id=222,
        direction="short",
        contracts=1,
        stop_price=25100.0,
        target_price=24700.0,
    )
    assert payload["action"] == "Sell"
    assert payload["contractId"] == 222
    assert "symbol" not in payload
    assert payload["bracket1"] == {
        "action": "Buy",
        "orderType": "Limit",
        "price": 24700.0,
    }
    assert payload["bracket2"] == {
        "action": "Buy",
        "orderType": "Stop",
        "stopPrice": 25100.0,
    }
    with pytest.raises(ValueError, match="stop_price < target_price"):
        build_bracket_entry_payload(
            account_id=42,
            contract_id="MNQZ6",
            direction="long",
            contracts=1,
            stop_price=25300.0,
            target_price=24900.0,
        )


def test_get_positions_and_orders_hit_documented_endpoints():
    adapter, transport = make_adapter(
        {
            "/position/list": (200, [{"id": 1, "accountId": 42, "contractId": 222, "netPos": 2}]),
            "/order/list": (200, [{"id": 5, "ordStatus": "Working"}]),
        }
    )
    assert adapter.get_positions()[0]["netPos"] == 2
    assert adapter.get_orders()[0]["ordStatus"] == "Working"
    assert transport.calls_to("/position/list")
    assert transport.calls_to("/order/list")


def test_flatten_all_liquidates_only_the_matching_position():
    positions = [
        {"id": 1, "accountId": 42, "contractId": 222, "netPos": 2},
        {"id": 2, "accountId": 42, "contractId": 999, "netPos": -1},
        {"id": 3, "accountId": 77, "contractId": 222, "netPos": 1},
        {"id": 4, "accountId": 42, "contractId": 222, "netPos": 0},
    ]
    adapter, transport = make_adapter(
        {
            "/position/list": (200, positions),
            "/order/liquidatePosition": (200, {"orderId": 55}),
        }
    )
    results = adapter.flatten_all(account_id=42, contract_id=222)
    assert results == [{"orderId": 55}]
    liquidations = transport.calls_to("/order/liquidatePosition")
    assert len(liquidations) == 1
    assert liquidations[0]["body"] == {
        "accountId": 42,
        "contractId": 222,
        "isAutomated": True,
    }


def test_flatten_all_with_no_open_position_sends_nothing():
    adapter, transport = make_adapter({"/position/list": (200, [])})
    assert adapter.flatten_all(account_id=42, contract_id=222) == []
    assert transport.calls_to("/order/liquidatePosition") == []


def test_order_failure_text_surfaces_as_api_error():
    adapter, _transport = make_adapter(
        {
            "/order/placeOSO": (
                200,
                {"failureReason": "UnknownReason", "failureText": "Insufficient margin"},
            )
        }
    )
    with pytest.raises(TradovateAPIError, match="Insufficient margin"):
        adapter.place_bracket_entry(
            account_id=42,
            contract_id="MNQZ6",
            direction="long",
            contracts=1,
            stop_price=24900.0,
            target_price=25300.0,
        )


def test_no_request_ever_goes_to_the_live_host():
    adapter, transport = make_adapter(
        {
            "/position/list": (200, []),
            "/order/list": (200, []),
            "/order/placeOSO": (200, {"orderId": 1, "oso1Id": 2, "oso2Id": 3}),
        }
    )
    adapter.get_positions()
    adapter.get_orders()
    adapter.place_bracket_entry(
        account_id=42,
        contract_id="MNQZ6",
        direction="long",
        contracts=1,
        stop_price=24900.0,
        target_price=25300.0,
    )
    assert transport.calls  # sanity: calls really happened
    for call in transport.calls:
        assert call["url"].startswith(DEMO_BASE_URL)
        assert "live.tradovateapi.com" not in call["url"]
    assert tv_config.LIVE_TRADING_ENABLED is False
