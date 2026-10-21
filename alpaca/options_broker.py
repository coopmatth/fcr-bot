"""Alpaca PAPER options: chain lookup, quote snapshots, orders.

Companion to alpaca/client.py for the ORB-15 OPTIONS variant
(ORB15_OPTIONS_SPEC.md). PAPER ONLY, enforced exactly as in the
shares code: :class:`OptionsClient` SUBCLASSES
:class:`alpaca.client.AlpacaClient`, so construction still
requires a paper-host config with a ``PK`` key, and every request
still passes the client's per-URL paper-host lock before it is
sent. Nothing here forks the host lock, the credential handling,
or the kill switch.

Endpoints (verified against Alpaca's official documentation,
https://docs.alpaca.markets):

* ``GET /v2/options/contracts`` (TRADING host, paper) — params
  ``underlying_symbols``, ``type`` ("call"/"put"),
  ``expiration_date_gte`` / ``expiration_date_lte`` (YYYY-MM-DD),
  ``status=active``, ``limit`` (max 1000) and ``page_token``
  pagination; the response is ``{"option_contracts": [...],
  "next_page_token": ...}``. Each contract carries ``symbol``
  (OCC), ``underlying_symbol``, ``type``, ``expiration_date``,
  ``strike_price`` (string), ``tradable``.
* ``GET /v1beta1/options/snapshots`` (DATA host) — params
  ``symbols`` (comma-joined OCC) and ``feed``; the free tier's
  feed is ``indicative`` and MAY BE DELAYED vs real-time OPRA
  (a paid add-on). Response: ``{"snapshots": {"<OCC>":
  {"latestQuote": {"bp", "ap", "bx", "ax", "t"}, "latestTrade":
  {...}, ...}}}`` — quote prices are PER SHARE, ``t`` is the
  quote's RFC3339 timestamp. The premium gate uses ``ap`` (ask)
  and refuses (see :meth:`OptionsBroker.fresh_quote`) when the
  snapshot is missing, has no usable ask, or is older than the
  caller's staleness bound — the runner never enters blind.
* Option ORDERS go through the same ``POST /v2/orders`` endpoint
  as equities, with the OCC symbol and qty in CONTRACTS; fills
  report ``filled_avg_price`` as the per-share premium. Position
  endpoints take the OCC symbol identically. Order placement,
  polling, partial-fill handling, cancel support, the F1
  client_order_id recovery and the close-position endpoint are
  therefore REUSED, not re-implemented: :class:`OptionsBroker`
  composes the shares runner's hardened ``AlpacaBroker`` over an
  ``OptionsClient``.
"""
from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Iterator, Optional
from urllib.parse import urlencode

from fcr.patterns import Bar

from .client import AlpacaClient
from .options_select import OptionContract, contract_from_api
from .runner import AlpacaBroker, Fill, OrderError

UTC = timezone.utc

OPTIONS_FEED = "indicative"  # the free options data feed
_CONTRACTS_PAGE_LIMIT = 1000
# Chain fetch windows for selection: SPY lists expiries densely
# (daily), so the nearest expiry on/after today is normally within
# days; query a fortnight first and widen once if it comes back
# empty (long holiday stretches), rather than paging a month of
# strikes on every signal.
CHAIN_WINDOW_DAYS = 14
CHAIN_WIDENED_WINDOW_DAYS = 62


class QuoteError(RuntimeError):
    """The premium gate cannot trust the quote it was offered."""


class QuoteUnavailableError(QuoteError):
    """No snapshot, no latestQuote, or no usable ask price."""


class StaleQuoteError(QuoteError):
    """The quote exists but is older than the staleness bound."""

    def __init__(self, message: str, age_minutes: Optional[float] = None):
        super().__init__(message)
        self.age_minutes = age_minutes


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    bid: Optional[float]   # per share; None when the book is one-sided
    ask: Optional[float]   # per share; None when unusable
    ts: Optional[datetime] # quote timestamp (aware, UTC) or None


def _parse_quote_ts(raw) -> Optional[datetime]:
    """Alpaca quote timestamp -> aware datetime. RFC3339, possibly
    with nanosecond digits and a Z suffix; unparseable -> None."""
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def quote_from_snapshot(symbol: str, entry) -> Optional[OptionQuote]:
    """One snapshots-payload entry -> OptionQuote, or None when the
    entry carries no latestQuote at all. An entry whose ask is
    missing/zero still yields a quote (ask=None): the gate treats
    that as unusable, and the distinction is worth logging."""
    if not isinstance(entry, dict):
        return None
    latest = entry.get("latestQuote")
    if not isinstance(latest, dict):
        return None

    def _price(value) -> Optional[float]:
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    return OptionQuote(
        symbol=symbol,
        bid=_price(latest.get("bp")),
        ask=_price(latest.get("ap")),
        ts=_parse_quote_ts(latest.get("t")),
    )


class OptionsClient(AlpacaClient):
    """AlpacaClient + the two options read endpoints. All inherited
    order/position methods accept OCC symbols unchanged."""

    def list_option_contracts(
        self,
        underlying: str,
        option_type: Optional[str] = None,
        on_or_after: Optional[date_type] = None,
        on_or_before: Optional[date_type] = None,
    ) -> list:
        """GET /v2/options/contracts (paper trading host), fully
        paginated -> the raw contract objects."""
        contracts: list = []
        page_token: Optional[str] = None
        while True:
            params = {
                "underlying_symbols": underlying,
                "status": "active",
                "limit": _CONTRACTS_PAGE_LIMIT,
            }
            if option_type:
                params["type"] = option_type
            if on_or_after is not None:
                params["expiration_date_gte"] = on_or_after.isoformat()
            if on_or_before is not None:
                params["expiration_date_lte"] = on_or_before.isoformat()
            if page_token:
                params["page_token"] = page_token
            url = self._trading(f"/v2/options/contracts?{urlencode(params)}")
            payload = self._request("GET", url)
            if not isinstance(payload, dict):
                return contracts
            contracts.extend(payload.get("option_contracts") or [])
            page_token = payload.get("next_page_token")
            if not page_token:
                return contracts

    def get_option_snapshot(self, contract_symbol: str) -> Optional[dict]:
        """GET /v1beta1/options/snapshots for one contract (data
        host, free indicative feed) -> the snapshot entry, or None
        when the response has no entry for the symbol."""
        query = urlencode(
            {"symbols": contract_symbol, "feed": OPTIONS_FEED}
        )
        url = f"{self.config.data_base_url}/v1beta1/options/snapshots?{query}"
        payload = self._request("GET", url)
        if not isinstance(payload, dict):
            return None
        snapshots = payload.get("snapshots")
        if not isinstance(snapshots, dict):
            return None
        entry = snapshots.get(contract_symbol)
        return entry if isinstance(entry, dict) else None


class OptionsBroker:
    """Broker facade for the options runner: the hardened
    AlpacaBroker's order mechanics (fill polling, partials, F1
    client_order_id recovery, cancel, close-position) over an
    OptionsClient, plus chain lookup and the premium gate's fresh-
    quote check. Quantities here are CONTRACTS; Fill.price is the
    per-share premium."""

    def __init__(
        self,
        client: OptionsClient,
        sleep_fn: Callable[[float], None] = time_module.sleep,
        poll_seconds: float = 1.0,
        max_polls: int = 30,
    ):
        self.client = client
        self._orders = AlpacaBroker(
            client,
            sleep_fn=sleep_fn,
            poll_seconds=poll_seconds,
            max_polls=max_polls,
        )

    # ------------------------------------------------- order mechanics
    def set_client_order_id(self, client_order_id: Optional[str]) -> None:
        self._orders.set_client_order_id(client_order_id)

    def position_qty(self, symbol: str) -> int:
        """Contracts held in ``symbol`` (0 when flat)."""
        return self._orders.position_qty(symbol)

    def place_market(self, symbol: str, qty: int, side: str) -> Fill:
        return self._orders.place_market(symbol, qty, side)

    def close_position(self, symbol: str) -> Fill:
        return self._orders.close_position(symbol)

    def list_positions(self) -> list:
        return self.client.list_positions()

    # ------------------------------------------------------------ chain
    def contracts_for_selection(
        self, underlying: str, today: date_type
    ) -> list[OptionContract]:
        """Parsed, selectable contracts for ``underlying`` with
        expiry in [today, today + CHAIN_WINDOW_DAYS]; widens once
        to CHAIN_WIDENED_WINDOW_DAYS when the first window is
        empty. Selection itself (options_select) re-filters by
        type/expiry/tradable — this is only the fetch."""
        for window in (CHAIN_WINDOW_DAYS, CHAIN_WIDENED_WINDOW_DAYS):
            raw = self.client.list_option_contracts(
                underlying,
                on_or_after=today,
                on_or_before=today + timedelta(days=window),
            )
            parsed = [
                c for c in (contract_from_api(r) for r in raw) if c is not None
            ]
            if parsed:
                return parsed
        return []

    # ------------------------------------------------------ premium gate
    def fresh_quote(
        self,
        symbol: str,
        now: datetime,
        max_staleness_minutes: float,
    ) -> OptionQuote:
        """The snapshot quote for ``symbol`` if — and only if — it
        is usable for the premium gate: present, carries an ask,
        and no older than ``max_staleness_minutes`` relative to
        ``now``. Raises QuoteUnavailableError / StaleQuoteError
        otherwise (ORB15_OPTIONS_SPEC.md rule 10)."""
        entry = self.client.get_option_snapshot(symbol)
        quote = quote_from_snapshot(symbol, entry) if entry else None
        if quote is None:
            raise QuoteUnavailableError(
                f"no options snapshot quote for {symbol}"
            )
        if quote.ask is None:
            raise QuoteUnavailableError(
                f"options snapshot for {symbol} has no usable ask "
                f"(bid={quote.bid})"
            )
        if quote.ts is None:
            raise QuoteUnavailableError(
                f"options snapshot for {symbol} has no quote timestamp"
            )
        age_minutes = (now - quote.ts).total_seconds() / 60.0
        if age_minutes > max_staleness_minutes:
            raise StaleQuoteError(
                f"options quote for {symbol} is {age_minutes:.1f} "
                f"minutes old (bound {max_staleness_minutes})",
                age_minutes=age_minutes,
            )
        return quote


# ----------------------------------------------------------------------
# Simulation (replay + tests) — mirrors runner.SimulatedBroker's shape:
# fills happen at the "current" premium set with set_premium, positions
# are tracked per contract symbol, quotes for the gate are set per
# symbol with set_quote.
# ----------------------------------------------------------------------
class SimulatedOptionsBroker:
    def __init__(self, premium: float = 0.0):
        self._premium = float(premium)
        self._positions: dict = {}   # OCC symbol -> contracts (signed)
        self._chain: list = []       # list[OptionContract]
        self._quotes: dict = {}      # OCC symbol -> OptionQuote | None
        self.orders: list = []
        self._seq = 0

    # ------------------------------------------------------------ setup
    def set_premium(self, premium: float) -> None:
        self._premium = float(premium)

    def set_chain(self, contracts: Iterable[OptionContract]) -> None:
        self._chain = list(contracts)

    def set_quote(self, symbol: str, quote: Optional[OptionQuote]) -> None:
        self._quotes[symbol] = quote

    def seed_position(self, symbol: str, qty_contracts: int) -> None:
        self._positions[symbol] = int(qty_contracts)

    # ---------------------------------------------------- broker protocol
    def set_client_order_id(self, client_order_id: Optional[str]) -> None:
        self._pending_cid = client_order_id

    def position_qty(self, symbol: str) -> int:
        return int(self._positions.get(symbol, 0))

    def list_positions(self) -> list:
        return [
            {"symbol": s, "qty": str(q), "side": "long" if q > 0 else "short"}
            for s, q in sorted(self._positions.items())
            if q != 0
        ]

    def place_market(self, symbol: str, qty: int, side: str) -> Fill:
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")
        self._seq += 1
        order_id = f"sim-opt-{self._seq}"
        self.orders.append(
            {"id": order_id, "symbol": symbol, "qty": qty, "side": side,
             "price": self._premium, "kind": "order"}
        )
        self._positions[symbol] = (
            self._positions.get(symbol, 0) + (qty if side == "buy" else -qty)
        )
        return Fill(price=self._premium, qty=qty, order_id=order_id)

    def close_position(self, symbol: str) -> Fill:
        """Close the whole position at the current premium. Kept
        distinct from place_market like the real API (and like
        SimulatedBroker): the runner's exit fallback depends on
        this path existing independently of plain orders."""
        qty_signed = self._positions.get(symbol, 0)
        if qty_signed == 0:
            raise OrderError(f"no {symbol} position to close")
        qty = abs(qty_signed)
        side = "sell" if qty_signed > 0 else "buy"
        self._seq += 1
        order_id = f"sim-opt-close-{self._seq}"
        self.orders.append(
            {"id": order_id, "symbol": symbol, "qty": qty, "side": side,
             "price": self._premium, "kind": "close_position"}
        )
        self._positions[symbol] = 0
        return Fill(price=self._premium, qty=qty, order_id=order_id)

    # ------------------------------------------------- options specifics
    def contracts_for_selection(
        self, underlying: str, today: date_type
    ) -> list:
        return [c for c in self._chain if c.underlying == underlying.upper()]

    def fresh_quote(
        self, symbol: str, now: datetime, max_staleness_minutes: float
    ) -> OptionQuote:
        """Same gate semantics as OptionsBroker.fresh_quote, over
        the quotes set with set_quote."""
        quote = self._quotes.get(symbol)
        if quote is None:
            raise QuoteUnavailableError(
                f"no options snapshot quote for {symbol}"
            )
        if quote.ask is None:
            raise QuoteUnavailableError(
                f"options snapshot for {symbol} has no usable ask "
                f"(bid={quote.bid})"
            )
        if quote.ts is None:
            raise QuoteUnavailableError(
                f"options snapshot for {symbol} has no quote timestamp"
            )
        age_minutes = (now - quote.ts).total_seconds() / 60.0
        if age_minutes > max_staleness_minutes:
            raise StaleQuoteError(
                f"options quote for {symbol} is {age_minutes:.1f} "
                f"minutes old (bound {max_staleness_minutes})",
                age_minutes=age_minutes,
            )
        return quote


def priced_option_bars(bars: Iterable[Bar], broker, premium_fn) -> Iterator[Bar]:
    """Yield bars while keeping a SimulatedOptionsBroker's fill
    premium at ``premium_fn(bar)`` — the options analogue of
    runner.priced_bars, for replay/tests."""
    for bar in bars:
        broker.set_premium(premium_fn(bar))
        yield bar
