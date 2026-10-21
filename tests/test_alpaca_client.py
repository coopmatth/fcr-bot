"""Mocked tests for the Alpaca PAPER-only client + config.

Run from the project root:  python3 -m pytest tests/ -q

NO real network calls: every client is built on FakeTransport. No
real credentials exist for this project; the values below are
obvious fakes (paper-style key IDs start with "PK").

Endpoint shapes asserted here are Alpaca's documented ones — see
the docstrings in alpaca/*.py for the doc references.
"""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from alpaca.client import AlpacaAPIError, AlpacaClient, parse_bar
from alpaca.config import (
    DATA_BASE_URL,
    LIVE_BASE_URL,
    PAPER_BASE_URL,
    AlpacaConfig,
    MissingCredentialsError,
    PaperOnlyError,
    runner_enabled,
)

ET = ZoneInfo("America/New_York")

FAKE_ENV = {
    "ALPACA_PAPER_KEY_ID": "PKFAKEPAPERKEYID",
    "ALPACA_PAPER_SECRET": "fake-paper-secret",
}


class FakeTransport:
    """Records requests; answers from a route table of
    (method, url-substring) -> (status, payload-dict-or-list)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers),
             "body": json.loads(body) if body else None}
        )
        for (m, needle), (status, payload) in self.routes.items():
            if m == method and needle in url:
                return status, {}, json.dumps(payload).encode()
        return 404, {}, json.dumps({"message": "no route"}).encode()


def make_client(routes):
    transport = FakeTransport(routes)
    client = AlpacaClient(AlpacaConfig.from_env(FAKE_ENV), transport=transport)
    return client, transport


# ----------------------------------------------------------------------
# config: credentials, host lock, key prefix, kill switch
# ----------------------------------------------------------------------
def test_config_from_env_ok():
    cfg = AlpacaConfig.from_env(FAKE_ENV)
    assert cfg.key_id == "PKFAKEPAPERKEYID"
    assert cfg.trading_base_url == PAPER_BASE_URL
    assert cfg.data_base_url == DATA_BASE_URL


def test_config_missing_credentials_names_both():
    with pytest.raises(MissingCredentialsError) as exc:
        AlpacaConfig.from_env({})
    assert "ALPACA_PAPER_KEY_ID" in str(exc.value)
    assert "ALPACA_PAPER_SECRET" in str(exc.value)


def test_config_missing_one_names_only_that_one():
    with pytest.raises(MissingCredentialsError) as exc:
        AlpacaConfig.from_env({"ALPACA_PAPER_KEY_ID": "PKX"})
    assert "ALPACA_PAPER_SECRET" in str(exc.value)
    # The present variable's VALUE never leaks into the error.
    assert "PKX" not in str(exc.value)


def test_config_refuses_live_host():
    with pytest.raises(PaperOnlyError):
        AlpacaConfig(
            key_id="PKFAKE",
            secret_key="fake",
            trading_base_url=LIVE_BASE_URL,
        )


def test_config_refuses_lookalike_host():
    with pytest.raises(PaperOnlyError):
        AlpacaConfig(
            key_id="PKFAKE",
            secret_key="fake",
            trading_base_url="https://paper-api.alpaca.markets.evil.example",
        )


def test_config_refuses_live_key_prefix():
    # Alpaca live key IDs start with "AK"; paper with "PK".
    with pytest.raises(PaperOnlyError):
        AlpacaConfig(key_id="AKFAKELIVEKEY", secret_key="fake")


def test_runner_enabled_defaults_off():
    assert runner_enabled({}) is False
    assert runner_enabled({"ALPACA_RUNNER_ENABLED": "0"}) is False
    assert runner_enabled({"ALPACA_RUNNER_ENABLED": "false"}) is False
    assert runner_enabled({"ALPACA_RUNNER_ENABLED": "1"}) is True
    assert runner_enabled({"ALPACA_RUNNER_ENABLED": "TRUE"}) is True
    assert runner_enabled({"ALPACA_RUNNER_ENABLED": " yes "}) is True


# ----------------------------------------------------------------------
# client: account / verify, headers, host lock at request time
# ----------------------------------------------------------------------
ACCOUNT = {"account_number": "PA123456789", "status": "ACTIVE",
           "cash": "100000.00", "portfolio_value": "100000.00"}


def test_get_account_hits_paper_host_with_auth_headers():
    client, transport = make_client(
        {("GET", "/v2/account"): (200, ACCOUNT)}
    )
    account = client.verify_paper()
    assert account["status"] == "ACTIVE"
    (call,) = transport.calls
    assert call["url"] == f"{PAPER_BASE_URL}/v2/account"
    assert call["headers"]["APCA-API-KEY-ID"] == "PKFAKEPAPERKEYID"
    assert call["headers"]["APCA-API-SECRET-KEY"] == "fake-paper-secret"


def test_request_to_live_host_refused_before_transport():
    client, transport = make_client({})
    with pytest.raises(PaperOnlyError):
        client._request("GET", f"{LIVE_BASE_URL}/v2/account")
    assert transport.calls == []  # never reached the transport


def test_error_response_maps_to_api_error():
    client, _ = make_client(
        {("GET", "/v2/account"): (403, {"message": "forbidden"})}
    )
    with pytest.raises(AlpacaAPIError) as exc:
        client.get_account()
    assert exc.value.status == 403
    assert "forbidden" in str(exc.value)


# ----------------------------------------------------------------------
# client: orders and positions
# ----------------------------------------------------------------------
ORDER_NEW = {"id": "ord-1", "status": "accepted", "filled_qty": "0",
             "filled_avg_price": None, "symbol": "SPY", "side": "buy"}


def test_place_market_order_body():
    client, transport = make_client(
        {("POST", "/v2/orders"): (200, ORDER_NEW)}
    )
    order = client.place_market_order("SPY", 254, "buy")
    assert order["id"] == "ord-1"
    (call,) = transport.calls
    assert call["url"] == f"{PAPER_BASE_URL}/v2/orders"
    assert call["body"] == {
        "symbol": "SPY", "qty": "254", "side": "buy",
        "type": "market", "time_in_force": "day",
    }


def test_place_market_order_rejects_bad_args():
    client, transport = make_client({})
    with pytest.raises(ValueError):
        client.place_market_order("SPY", 0, "buy")
    with pytest.raises(ValueError):
        client.place_market_order("SPY", 10, "hold")
    assert transport.calls == []


def test_place_market_order_with_client_order_id_body():
    # Hardening F1: the deterministic client_order_id rides in the
    # body when provided (and only then — see the body test above).
    client, transport = make_client(
        {("POST", "/v2/orders"): (200, ORDER_NEW)}
    )
    client.place_market_order(
        "SPY", 93, "buy", client_order_id="orb15-2026-10-21-entry"
    )
    (call,) = transport.calls
    assert call["body"]["client_order_id"] == "orb15-2026-10-21-entry"
    assert call["body"]["qty"] == "93"


def test_get_order_by_client_order_id_url():
    client, transport = make_client(
        {("GET", "/v2/orders:by_client_order_id"): (200, ORDER_NEW)}
    )
    order = client.get_order_by_client_order_id("orb15-2026-10-21-entry")
    assert order["id"] == "ord-1"
    (call,) = transport.calls
    assert call["url"] == (
        f"{PAPER_BASE_URL}/v2/orders:by_client_order_id"
        "?client_order_id=orb15-2026-10-21-entry"
    )


def test_cancel_order_uses_delete():
    # Hardening F2: the cancel path exists and is a DELETE on the
    # order resource.
    client, transport = make_client(
        {("DELETE", "/v2/orders/ord-1"): (200, {"id": "ord-1",
                                                "status": "canceled"})}
    )
    result = client.cancel_order("ord-1")
    assert result["status"] == "canceled"
    (call,) = transport.calls
    assert call["method"] == "DELETE"
    assert call["url"] == f"{PAPER_BASE_URL}/v2/orders/ord-1"


def test_get_calendar_url_and_payload():
    # Hardening F6: calendar entries pass through as a list.
    entries = [{"date": "2026-11-27", "open": "09:30",
                "close": "13:00", "session_open": "0930",
                "session_close": "1300"}]
    client, transport = make_client(
        {("GET", "/v2/calendar"): (200, entries)}
    )
    from datetime import date as _date

    got = client.get_calendar(_date(2026, 11, 27), _date(2026, 11, 27))
    assert got == entries
    (call,) = transport.calls
    assert call["url"] == (
        f"{PAPER_BASE_URL}/v2/calendar"
        "?start=2026-11-27&end=2026-11-27"
    )


def test_get_position_404_means_flat():
    client, _ = make_client(
        {("GET", "/v2/positions/SPY"): (404, {"message": "not found"})}
    )
    assert client.get_position("SPY") is None


def test_get_position_returns_object():
    pos = {"symbol": "SPY", "qty": "-93", "side": "short"}
    client, _ = make_client({("GET", "/v2/positions/SPY"): (200, pos)})
    assert client.get_position("SPY")["qty"] == "-93"


# ----------------------------------------------------------------------
# client: bars — parsing, feed, pagination
# ----------------------------------------------------------------------
def _bar_payload(ts, close, token=None):
    return {
        "bars": [
            {"t": ts, "o": close - 0.1, "h": close + 0.2,
             "l": close - 0.2, "c": close, "v": 12345}
        ],
        "next_page_token": token,
        "symbol": "SPY",
    }


def test_parse_bar_converts_utc_to_et():
    bar = parse_bar({"t": "2026-10-21T13:30:00Z", "o": 1, "h": 2,
                     "l": 0.5, "c": 1.5, "v": 10})
    assert bar.ts == datetime(2026, 10, 21, 9, 30, tzinfo=ET)
    assert bar.close == 1.5


def test_get_bars_uses_data_host_iex_raw_and_paginates():
    page1 = _bar_payload("2026-10-21T13:30:00Z", 100.0, token="tok2")
    page2 = _bar_payload("2026-10-21T13:31:00Z", 100.5, token=None)

    calls = {"n": 0}

    def transport(method, url, headers, body):
        calls["n"] += 1
        payload = page1 if "page_token" not in url else page2
        return 200, {}, json.dumps(payload).encode()

    client = AlpacaClient(AlpacaConfig.from_env(FAKE_ENV), transport=transport)
    start = datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)
    bars = client.get_bars("SPY", start, end)
    assert calls["n"] == 2
    assert [b.close for b in bars] == [100.0, 100.5]
    assert bars[0].ts == datetime(2026, 10, 21, 9, 30, tzinfo=ET)


def test_get_bars_query_params():
    seen = {}

    def transport(method, url, headers, body):
        seen["url"] = url
        return 200, {}, json.dumps({"bars": [], "next_page_token": None}).encode()

    client = AlpacaClient(AlpacaConfig.from_env(FAKE_ENV), transport=transport)
    start = datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)
    client.get_bars("SPY", start, end)
    url = seen["url"]
    assert url.startswith(f"{DATA_BASE_URL}/v2/stocks/SPY/bars?")
    assert "timeframe=1Min" in url
    assert "feed=iex" in url
    assert "adjustment=raw" in url
