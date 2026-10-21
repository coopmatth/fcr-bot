"""Tradovate market-data adapter: MNQ contracts + 1-minute bars.

Output is exactly what the FCR engine eats: ``fcr.patterns.Bar``
(frozen dataclass — tz-aware ``ts``, ``open``/``high``/``low``/
``close`` floats, ``volume`` float). Timestamps are converted to
``America/New_York`` because the engine compares ``bar.ts.time()``
against ET session times from config.yaml.

Verified against the official documentation:

* Market Data API — ``md/getChart`` over the market-data WebSocket,
  request ``{symbol, chartDescription: {underlyingType: "MinuteBar",
  elementSize, elementSizeUnit: "UnderlyingUnits", withHistogram},
  timeRange: {asMuchAsElements, ...}}``; response carries
  ``historicalId`` / ``realtimeId``; cancel with ``md/cancelChart``
  ``{subscriptionId}``. Data messages are ``{"e": "chart", "d":
  {"charts": [{"id", "td", "bars": [{timestamp, open, high, low,
  close, upVolume, downVolume, upTicks, downTicks, bidVolume,
  offerVolume}]}]}}``:
  https://docs.ninjatrader.com/market-data
  (fetched as https://docs.ninjatrader.com/market-data)
* WebSocket framing — server frames are a one-character prefix
  (``o`` open, ``h`` heartbeat, ``a`` array, ``c`` close) + JSON;
  client requests are ``endpoint\\n<id>\\n<query>\\n<body>``;
  authorize once with ``authorize\\n<id>\\n\\n<token>``; client
  heartbeat is the literal text ``[]`` every ~2.5 s:
  https://docs.ninjatrader.com/api/websockets
* Market-data socket URL ``wss://md.tradovateapi.com/v1/websocket``
  and the ``md/getchart`` send pattern: official sample code quoted at
  https://community.tradovate.com/t/get-quote-tick-data/7809/4
* Contract lookup — ``GET /contract/find?name=<full name>`` returns
  the contract entity (``id``, ``name``, ``contractMaturityId``);
  ``GET /contract/suggest?t=<root>`` lists a root's contract months:
  find/suggest usage in Tradovate's own sample code at
  https://community.tradovate.com/t/not-found-md-subscribequote/4451
  and the suggest URL at
  https://community.tradovate.com/t/how-to-get-the-current-month-mes-ticker/4280
* Bar volume: a chart bar carries no single ``volume`` field. The
  community answer from Tradovate staff is that total volume is a
  sum of the bar's volume components:
  https://community.tradovate.com/t/confirmation-on-md-getchart-parameters-and-volume/3740
  This adapter sums ``upVolume + downVolume`` when present, else
  ``bidVolume + offerVolume``, else a literal ``volume`` field,
  else 0.0. (The engine ignores volume; this is for logging.)
* Contract month codes F/G/H/J/K/M/N/Q/U/V/X/Z = Jan..Dec and the
  one-digit year suffix (e.g. ``MNQZ6`` = Dec 2026):
  https://help.signalstack.com/kb/tradovate/tradovate-future

The socket is behind a tiny injectable interface — any object with
``send(text)`` and ``recv() -> text`` — so bar parsing and the
subscription handshake are unit-testable with a fake socket and no
network, no third-party websocket library, and no real account.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Iterable, Iterator, List, Optional, Protocol, Tuple
from zoneinfo import ZoneInfo

from fcr.patterns import Bar

from .client import TradovateAPIError, TradovateClient

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# Month code -> month number, per the futures month-code table above.
MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

# Client heartbeat frame text (WebSocket docs: literal "[]").
HEARTBEAT_FRAME = "[]"


class SocketLike(Protocol):
    """Minimal socket surface this adapter needs (duck-typed)."""

    def send(self, text: str) -> None: ...

    def recv(self) -> str: ...


# ------------------------------------------------------------ ws framing
def format_ws_request(endpoint: str, request_id: int, body=None, query: str = "") -> str:
    """One client request frame: ``endpoint\\n<id>\\n<query>\\n<body>``.

    With no query this is the ``endpoint\\n<id>\\n\\n<json>`` shape
    shown in both the WebSocket docs and the official samples."""
    body_text = "" if body is None else (body if isinstance(body, str) else json.dumps(body))
    return f"{endpoint}\n{request_id}\n{query}\n{body_text}"


def format_authorize(request_id: int, token: str) -> str:
    """The one-time socket authorization frame (token as the body)."""
    return format_ws_request("authorize", request_id, body=token)


def parse_ws_frame(raw) -> Tuple[str, Optional[list]]:
    """Split a server frame into (type_char, payload list | None).

    Mirrors the docs' ``prepareMsg``: first character is the type
    (``o``/``h``/``a``/``c``), the remainder (if any) is a JSON array.
    Already-decoded lists/dicts are passed through as ``a`` payloads.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, list):
        return "a", raw
    if isinstance(raw, dict):
        return "a", [raw]
    text = str(raw)
    if not text:
        return "", None
    frame_type, rest = text[0], text[1:]
    if not rest:
        return frame_type, None
    return frame_type, json.loads(rest)


# ------------------------------------------------------------ bar parsing
def _parse_timestamp(value) -> datetime:
    """Documented bar timestamp (ISO-8601 UTC) -> aware ET datetime."""
    if isinstance(value, (int, float)):  # epoch seconds/ms, defensive
        seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC).astimezone(ET)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(ET)


def _bar_volume(raw: dict) -> float:
    if raw.get("upVolume") is not None and raw.get("downVolume") is not None:
        return float(raw["upVolume"]) + float(raw["downVolume"])
    if raw.get("bidVolume") is not None and raw.get("offerVolume") is not None:
        return float(raw["bidVolume"]) + float(raw["offerVolume"])
    if raw.get("volume") is not None:
        return float(raw["volume"])
    return 0.0


def parse_bar(raw: dict) -> Bar:
    """One documented chart ``bars[]`` object -> ``fcr.patterns.Bar``."""
    return Bar(
        ts=_parse_timestamp(raw["timestamp"]),
        open=float(raw["open"]),
        high=float(raw["high"]),
        low=float(raw["low"]),
        close=float(raw["close"]),
        volume=_bar_volume(raw),
    )


def bars_from_payload(payload) -> List[Bar]:
    """Extract every Bar from one decoded message object.

    Accepts the documented event shape ``{"e": "chart", "d":
    {"charts": [...]}}``, a request-response shape ``{"s": 200,
    "i": n, "d": {...}}`` whose ``d`` holds ``charts``, or a bare
    ``{"charts": [...]}`` dict. Non-chart messages yield []."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("d") if isinstance(payload.get("d"), dict) else payload
    charts = data.get("charts") if isinstance(data, dict) else None
    if not charts:
        return []
    bars: List[Bar] = []
    for chart in charts:
        for raw_bar in chart.get("bars", []) or []:
            bars.append(parse_bar(raw_bar))
    return bars


def bars_from_frame(raw) -> List[Bar]:
    """Bars from one raw server frame (SockJS text, bytes, or decoded)."""
    frame_type, payload = parse_ws_frame(raw)
    if frame_type not in ("a", "") or not payload:
        return []
    bars: List[Bar] = []
    for item in payload:
        bars.extend(bars_from_payload(item))
    return bars


def parse_chart_messages(messages: Iterable) -> Iterator[Bar]:
    """Yield Bars from an iterable of raw/decoded socket messages."""
    for message in messages:
        yield from bars_from_frame(message)


# ------------------------------------------------------- chart subscription
def build_get_chart_body(symbol, as_much_as_elements: int = 100) -> dict:
    """The documented ``md/getChart`` body for 1-minute bars."""
    return {
        "symbol": symbol,
        "chartDescription": {
            "underlyingType": "MinuteBar",
            "elementSize": 1,
            "elementSizeUnit": "UnderlyingUnits",
            "withHistogram": False,
        },
        "timeRange": {"asMuchAsElements": as_much_as_elements},
    }


def build_cancel_chart_body(subscription_id: int) -> dict:
    """The documented ``md/cancelChart`` body."""
    return {"subscriptionId": subscription_id}


# ------------------------------------------------------------ contract help
def parse_contract_expiry(name: str, root: str, today: Optional[date] = None):
    """(year, month) a contract name like ``MNQZ6`` expires in.

    The year is a single digit; it is resolved to the year with that
    digit closest to ``today``'s year (within the surrounding decade
    window). Returns None when the name doesn't parse as
    ``<root><monthCode><digit>``."""
    today = today or date.today()
    if not name.startswith(root) or len(name) != len(root) + 2:
        return None
    month = MONTH_CODES.get(name[len(root)])
    digit = name[-1]
    if month is None or not digit.isdigit():
        return None
    base = today.year - (today.year % 10) + int(digit)
    candidates = [base - 10, base, base + 10]
    year = min(candidates, key=lambda y: abs(y - today.year))
    return year, month


class MarketDataAdapter:
    """Contract resolution (REST) + chart bars (market-data socket)."""

    def __init__(self, client: TradovateClient):
        self.client = client

    # ------------------------------------------------------------ contracts
    def find_contract(self, name: str) -> dict:
        """GET /contract/find?name=<full contract name>."""
        result = self.client.get("/contract/find", params={"name": name})
        if not isinstance(result, dict) or result.get("id") is None:
            raise TradovateAPIError(
                f"contract/find returned no contract for {name!r}.",
                payload=result,
            )
        return result

    def suggest_contracts(self, root: str) -> List[dict]:
        """GET /contract/suggest?t=<root> — all listed months for a root."""
        result = self.client.get("/contract/suggest", params={"t": root})
        if isinstance(result, dict):  # defensive: some shapes wrap the list
            result = result.get("contracts") or result.get("items") or []
        return list(result or [])

    def resolve_front_month(
        self, root: str = "MNQ", today: Optional[date] = None
    ) -> dict:
        """The current front-month contract dict for ``root``.

        Tradovate rolls its continuous contracts on volume, which no
        REST payload here exposes, so this uses the closest verifiable
        rule: among ``contract/suggest`` results, the earliest expiry
        month that is not in the past. If a suggest item ever carries
        an explicit expiration date field it wins over name parsing.
        Callers that need certainty should confirm the returned
        ``name`` against the trader app before trading it."""
        today = today or date.today()
        candidates = self.suggest_contracts(root)
        if not candidates:
            raise TradovateAPIError(
                f"contract/suggest returned no contracts for root {root!r}."
            )
        scored = []
        for item in candidates:
            expiry = None
            for key in ("expirationDate", "expiryDate", "expiration"):
                if item.get(key):
                    try:
                        expiry_date = _parse_timestamp(item[key]).date()
                        expiry = (expiry_date.year, expiry_date.month)
                    except (ValueError, TypeError):
                        expiry = None
                    break
            if expiry is None and item.get("name"):
                expiry = parse_contract_expiry(str(item["name"]), root, today)
            if expiry is not None:
                scored.append((expiry, item))
        if not scored:
            return candidates[0]
        future = [s for s in scored if s[0] >= (today.year, today.month)]
        pool = future if future else scored
        pool.sort(key=lambda s: s[0])
        return pool[0][1]

    # ----------------------------------------------------------------- bars
    def stream_bars(
        self,
        socket: SocketLike,
        symbol,
        md_access_token: str,
        as_much_as_elements: int = 100,
        max_messages: Optional[int] = None,
    ) -> Iterator[Bar]:
        """Drive one market-data socket: authorize, ``md/getChart``
        for 1-minute bars, yield Bars as chart frames arrive.

        ``socket`` is injectable (see :class:`SocketLike`); a real
        deployment passes a thin wrapper around a websocket client
        connected to ``config.market_data_ws_url``, tests pass a
        fake. Note the documented realtime behaviour: the in-progress
        bar is re-emitted with the same timestamp as it builds, and
        the bar is final once the timestamp advances — consumers
        feeding the FCR engine should hand it each timestamp's last
        update (the backtester/engine semantics assume closed bars).
        """
        request_id = 0
        socket.send(format_authorize(request_id, md_access_token))
        request_id += 1
        socket.send(
            format_ws_request(
                "md/getChart",
                request_id,
                body=build_get_chart_body(symbol, as_much_as_elements),
            )
        )
        request_id += 1
        received = 0
        while max_messages is None or received < max_messages:
            try:
                raw = socket.recv()
            except (StopIteration, EOFError):
                return
            if raw is None:
                return
            received += 1
            frame_type, _payload = parse_ws_frame(raw)
            if frame_type == "h":
                socket.send(HEARTBEAT_FRAME)
                continue
            if frame_type == "c":
                return
            yield from bars_from_frame(raw)
