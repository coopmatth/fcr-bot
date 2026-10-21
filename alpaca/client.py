"""Alpaca PAPER REST client: account, orders, positions, SPY bars.

Thin wrapper over the Alpaca PAPER trading API
(``https://paper-api.alpaca.markets/v2``) and the market-data API
(``https://data.alpaca.markets/v2``). HTTP uses ONLY the Python
standard library (urllib) in production, and every request goes
through an injectable ``transport`` callable so unit tests run with a
fake transport and never touch the network.

Transport contract::

    transport(method, url, headers, body_bytes)
        -> (status_code: int, headers: Mapping, body: bytes)

Every outgoing URL is re-checked against the paper host lock
(:func:`alpaca.config.assert_allowed_url`) immediately before the
transport runs — defense in depth behind the config-level lock.

Verified against Alpaca's official documentation
(https://docs.alpaca.markets):

* Auth: headers ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` on
  every request (both APIs).
* ``GET /v2/account`` — account object (status, cash, ...). There is
  no "paper" flag on the account object itself; the paper environment
  IS the paper host, which is why the host lock is the guarantee.
* ``POST /v2/orders`` — body ``symbol`` / ``qty`` / ``side`` /
  ``type="market"`` / ``time_in_force="day"`` creates a market order;
  the response order carries ``id``, ``status``, ``filled_qty`` and
  ``filled_avg_price`` (strings; the price is null until filled).
* ``GET /v2/orders/{id}`` — poll one order.
* ``GET /v2/positions`` / ``GET /v2/positions/{symbol}`` — open
  positions; ``qty`` is a SIGNED string (negative = short). A missing
  position is HTTP 404. ``DELETE /v2/positions/{symbol}`` closes it
  with a market order.
* ``GET /v2/stocks/{symbol}/bars`` — params ``timeframe=1Min``,
  ``start`` / ``end`` (RFC3339), ``feed``, ``adjustment=raw``,
  ``limit`` (max 10000), ``sort=asc``; the response is
  ``{"bars": [{"t","o","h","l","c","v",...}], "next_page_token"}``
  where ``t`` is the bar's start time in UTC. Pagination follows
  ``next_page_token`` via the ``page_token`` parameter.

FEED NOTE (see alpaca/runner.py for the full text): the free
real-time data feed is ``feed=iex`` — IEX only, roughly 3% of
consolidated volume. The bars this client returns are therefore NOT
the Polygon SIP bars the ORB-15 backtest validated on. ``adjustment``
is pinned to ``raw`` so prices are the as-traded prices, matching the
backtest's raw Polygon bars.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fcr.patterns import Bar

from .config import AlpacaConfig, assert_allowed_url

# transport(method, url, headers, body) -> (status, headers, body_bytes)
Transport = Callable[
    [str, str, Mapping[str, str], Optional[bytes]],
    Tuple[int, Mapping[str, str], bytes],
]

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

DEFAULT_FEED = "iex"
_BARS_PAGE_LIMIT = 10000


class AlpacaAPIError(Exception):
    """An error response from Alpaca (or a transport failure wrapped
    with context). Carries the HTTP status when there is one."""

    def __init__(self, message: str, status: Optional[int] = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects outright (hardening F13).

    The default urllib opener follows 30x responses — including to
    a different host — and re-sends our APCA credential headers.
    A paper-API redirect should never happen; if one does, fail
    loudly instead of leaking the keys anywhere. Mirrors the
    Databento connector helper's NoRedirect pattern."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN202
        raise AlpacaAPIError(
            f"redirect refused (HTTP {code} -> {newurl}): credential "
            "headers must never follow a redirect off the locked host"
        )


_REDIRECT_SAFE_OPENER = urllib.request.build_opener(_NoRedirect())


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
) -> Tuple[int, Mapping[str, str], bytes]:
    """Default transport: one urllib request. Never used in tests.
    Redirects are refused (see _NoRedirect)."""
    req = urllib.request.Request(url=url, data=body, method=method.upper())
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with _REDIRECT_SAFE_OPENER.open(req, timeout=30) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:  # HTTP status >= 400 is a response
        return exc.code, dict(exc.headers.items() if exc.headers else {}), exc.read()
    except urllib.error.URLError as exc:
        raise AlpacaAPIError(f"Network error calling Alpaca: {exc.reason}") from exc


def _decode_json(status: int, body: bytes):
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AlpacaAPIError(
            f"Alpaca returned a non-JSON response (HTTP {status}).",
            status=status,
        ) from exc


def _rfc3339(dt: datetime) -> str:
    """RFC3339 in UTC with a Z suffix, as the data API expects."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_bar(raw: Mapping) -> Bar:
    """One data-API bar object -> fcr.patterns.Bar (ET-aware ts).

    The API's ``t`` is the bar START time in UTC; the project's Bar
    convention is the same instant expressed in America/New_York."""
    ts = datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return Bar(
        ts=ts.astimezone(ET),
        open=float(raw["o"]),
        high=float(raw["h"]),
        low=float(raw["l"]),
        close=float(raw["c"]),
        volume=float(raw.get("v") or 0.0),
    )


def bars_from_payload(payload) -> list[Bar]:
    """A /v2/stocks/{symbol}/bars response -> Bars, ascending."""
    if not isinstance(payload, dict):
        return []
    return [parse_bar(b) for b in payload.get("bars") or []]


class AlpacaClient:
    """Paper trading + data client. Construction refuses anything
    non-paper via AlpacaConfig's own validation."""

    def __init__(
        self,
        config: AlpacaConfig,
        transport: Optional[Transport] = None,
    ):
        self.config = config
        self._transport: Transport = transport or urllib_transport

    # ------------------------------------------------------------ internals
    def _headers(self, has_body: bool) -> dict:
        headers = {
            "Accept": "application/json",
            "APCA-API-KEY-ID": self.config.key_id,
            "APCA-API-SECRET-KEY": self.config.secret_key,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(self, method: str, url: str, body_obj=None):
        # Host lock re-checked on EVERY request, immediately before
        # the transport runs: only the paper trading host and the
        # data host may ever be called by this package.
        assert_allowed_url(url)
        body = None
        if body_obj is not None:
            body = json.dumps(body_obj).encode("utf-8")
        status, _resp_headers, raw = self._transport(
            method, url, self._headers(body is not None), body
        )
        payload = _decode_json(status, raw)
        if status >= 400:
            message = None
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error")
            raise AlpacaAPIError(
                f"Alpaca API error (HTTP {status}): {message or 'request failed'}",
                status=status,
                payload=payload,
            )
        return payload

    def _trading(self, path: str) -> str:
        return f"{self.config.trading_base_url}{path}"

    # ----------------------------------------------------------------- account
    def get_account(self) -> dict:
        """GET /v2/account on the PAPER host."""
        payload = self._request("GET", self._trading("/v2/account"))
        if not isinstance(payload, dict):
            raise AlpacaAPIError("Alpaca account response was not an object.")
        return payload

    def verify_paper(self) -> dict:
        """Prove this client can only be talking to the paper account.

        The guarantee is structural — config refused a non-paper host
        and a detectably-live key at construction, and _request
        re-checks every URL — so this simply fetches the account from
        the paper host and returns it for the caller's logs."""
        return self.get_account()

    # ------------------------------------------------------------------ orders
    def place_market_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """POST /v2/orders: a day market order for ``qty`` shares.

        ``side`` is "buy" or "sell" (a short entry is a sell; a short
        exit is a buy — the caller owns that mapping). Returns the
        order object; it is usually not yet filled — poll
        :meth:`get_order` for ``filled_avg_price``.

        ``client_order_id`` (hardening F1) makes a submission
        idempotent and recoverable: the caller picks a deterministic
        id, and if the POST's outcome is ever unknown (timeout,
        dropped connection) the order can be looked up with
        :meth:`get_order_by_client_order_id` instead of guessing.
        Included in the body only when provided."""
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        body = {
            "symbol": symbol,
            "qty": str(int(qty)),
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        if client_order_id is not None:
            body["client_order_id"] = str(client_order_id)
        payload = self._request("POST", self._trading("/v2/orders"), body)
        if not isinstance(payload, dict) or not payload.get("id"):
            raise AlpacaAPIError(
                "Alpaca order response did not include an order id.",
                payload=payload,
            )
        return payload

    def get_order(self, order_id: str) -> dict:
        """GET /v2/orders/{id}."""
        payload = self._request("GET", self._trading(f"/v2/orders/{order_id}"))
        if not isinstance(payload, dict):
            raise AlpacaAPIError("Alpaca order response was not an object.")
        return payload

    def get_order_by_client_order_id(self, client_order_id: str) -> dict:
        """GET /v2/orders:by_client_order_id — the F1 recovery path:
        after a submission whose outcome is unknown, find the order
        the submission created (Alpaca answers 404 when none exists)."""
        query = urlencode({"client_order_id": str(client_order_id)})
        payload = self._request(
            "GET", self._trading(f"/v2/orders:by_client_order_id?{query}")
        )
        if not isinstance(payload, dict):
            raise AlpacaAPIError("Alpaca order response was not an object.")
        return payload

    def cancel_order(self, order_id: str) -> dict:
        """DELETE /v2/orders/{id} — cancel an order's unfilled
        remainder (hardening F2). Returns the canceled order object
        when Alpaca sends one; an empty 204 body maps to {}."""
        payload = self._request(
            "DELETE", self._trading(f"/v2/orders/{order_id}")
        )
        return payload if isinstance(payload, dict) else {}

    # --------------------------------------------------------------- positions
    def list_positions(self) -> list:
        """GET /v2/positions -> list of position objects."""
        payload = self._request("GET", self._trading("/v2/positions"))
        return payload if isinstance(payload, list) else []

    def get_position(self, symbol: str) -> Optional[dict]:
        """GET /v2/positions/{symbol} -> position object or None.

        Alpaca answers 404 when there is no open position; that is a
        normal state, not an error."""
        try:
            payload = self._request(
                "GET", self._trading(f"/v2/positions/{symbol}")
            )
        except AlpacaAPIError as exc:
            if exc.status == 404:
                return None
            raise
        return payload if isinstance(payload, dict) else None

    def close_position(self, symbol: str) -> dict:
        """DELETE /v2/positions/{symbol} — market-close the position.
        Returns the closing order object."""
        payload = self._request(
            "DELETE", self._trading(f"/v2/positions/{symbol}")
        )
        if not isinstance(payload, dict):
            raise AlpacaAPIError("Alpaca close-position response was not an object.")
        return payload

    # ---------------------------------------------------------------- calendar
    def get_calendar(self, start, end) -> list:
        """GET /v2/calendar?start=YYYY-MM-DD&end=YYYY-MM-DD
        (hardening F6) -> list of {"date", "open", "close", ...}
        entries, one per TRADING day in the inclusive range; dates
        with no entry are non-trading days. ``start``/``end`` may be
        date or datetime objects."""
        def _fmt(d) -> str:
            return d.isoformat()[:10] if not isinstance(d, str) else d

        query = urlencode({"start": _fmt(start), "end": _fmt(end)})
        payload = self._request("GET", self._trading(f"/v2/calendar?{query}"))
        return payload if isinstance(payload, list) else []

    # -------------------------------------------------------------------- bars
    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        feed: str = DEFAULT_FEED,
    ) -> list[Bar]:
        """GET /v2/stocks/{symbol}/bars, 1-minute, fully paginated.

        ``start``/``end`` are aware datetimes (any tz; sent as UTC
        RFC3339). Returns Bars ascending by start time. Adjustment is
        pinned to "raw" (as-traded prices, matching the backtest)."""
        bars: list[Bar] = []
        page_token: Optional[str] = None
        while True:
            params = {
                "timeframe": "1Min",
                "start": _rfc3339(start),
                "end": _rfc3339(end),
                "feed": feed,
                "adjustment": "raw",
                "limit": _BARS_PAGE_LIMIT,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            url = (
                f"{self.config.data_base_url}/v2/stocks/{symbol}/bars"
                f"?{urlencode(params)}"
            )
            payload = self._request("GET", url)
            bars.extend(bars_from_payload(payload))
            page_token = (
                payload.get("next_page_token") if isinstance(payload, dict) else None
            )
            if not page_token:
                return bars
