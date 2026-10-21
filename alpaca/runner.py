"""ORB-15 forward-test runner on the Alpaca PAPER account.

PAPER ONLY. This runner trades exactly one thing — SPY — with the
strategy frozen in ``ORB15_SPEC.md`` (the project's root): 15-minute
opening-range breakout, first close beyond the range before 12:00 ET,
stop at the opposite edge, target 2R, shares = floor($150 / risk
distance), one trade per day, flat by 15:55 ET.

DESIGN — the runner IS the frozen strategy, not a re-implementation.
Signal decisions (range, trigger, sizing, stop/target, exit bar and
exit reason) come from ``fcr.strategies_intraday.ORBStrategy`` — the
very class the validating backtest ran — fed with completed 1-minute
bars. The runner owns everything the backtest simulated away:

* fills: on a strategy signal it places a PAPER market order and
  books the ACTUAL fill price, which will differ from the backtest's
  bar-close / exact-level fills. That difference is the forward
  test's subject, not an error.
* exits: runner-managed. The strategy decides WHICH bar exits and
  WHY (stop touch / target touch / 15:55 flatten, stop wins a
  same-bar tie — the spec's bar semantics); the runner then sends
  the market order and books the real fill.
* accounting: a $500 "overlay" account tracked from realized fills,
  starting at $500 like the backtest's paper account. The Alpaca
  paper account's own balance ($100k) is fill machinery only —
  sizing always follows the $150-risk spec. Each closed trade also
  reports pnl net of the backtest's $0.04/share friction model so
  forward results stay roughly comparable to the AF verdicts.
* persistence: every event (signal, order, fill, exit, daily
  summary, halt) is appended to a JSONL log, and a small JSON state
  file is rewritten after every transition, so a restart resumes
  the day instead of forgetting it.
* reconciliation: at day start the runner compares its state file
  with the paper account's actual SPY position. ANY disagreement it
  cannot fully explain — the account holds SPY the state doesn't
  know about, the state claims a position the account doesn't hold,
  quantities differ — HALTS the runner (state saved with the halt
  reason, no orders placed). It never guesses. A position that
  matches state exactly after a mid-day restart is adopted: the
  day's bars are replayed through the strategy to rebuild decision
  state, the strategy's re-fired entry is checked against the
  recorded one, and trading continues. If the bars can't rebuild
  the strategy's position (feed gap), the runner still manages the
  known position itself from the recorded stop/target levels and
  flattens it by 15:55 — that fallback engages only once no
  (re-)entry can fire (the 12:00 entry window has closed or the
  strategy is done), because pre-entry bars during a rebuild are
  history the position must not be managed against.
* live-frontier gate: a day run may be handed a ``live_since``
  frontier — the moment the runner began consuming the day live
  (``serve()`` always passes the wall-clock moment it starts the
  day). Bars already complete at the frontier are start-of-day
  catch-up: the strategy still replays them (its state must be
  rebuilt from them), but a signal they produce is HISTORY and
  can never place an order. Such a day resolves as a
  ``missed_day`` — the replayed outcome is logged for the record
  and nothing is booked — whether the replayed trade already
  closed during catch-up or is still open at the frontier (the
  runner never enters late). Only a signal on a bar that
  completes AFTER the frontier can place an entry order, and the
  check lives in the entry placement itself, so no code path can
  bypass it. Exits are never gated: a position adopted at
  restart is real whenever its decision bars occurred. Offline
  replay/tests pass no frontier (``live_since=None``): the
  runner is deemed live for every bar, the semantics the CSV
  replay proof relies on. (Why this exists: on 2026-10-21 the
  service was first started at 14:32 ET and the un-gated runner
  replayed the morning backfill and EXECUTED the 09:47 signal
  with live market orders at the 14:32 price — see the README's
  forward-test section.)

SAFETY — hard-enforced, not configurable:

* Paper only: alpaca.config refuses any non-paper host and any
  detectably-live key at construction; the client re-checks every
  URL before it is sent.
* Kill switch: orders are placed only when the runner is built with
  ``enabled=True`` (``ALPACA_RUNNER_ENABLED`` env flag, default
  False). Disabled, the runner still processes bars and logs every
  signal as a blocked would-be order — but the day ends there.
  The kill switch blocks ENTRIES; a known position is always
  flattened (exits are risk-reducing, and reconciliation has already
  proven the position is ours and understood).
* At most one open position, at most one trade per day, no
  overnight positions: enforced by the strategy (one trade/day)
  plus the runner's end-of-day and overnight-leftover flattening.

FEED NOTE — READ BEFORE TRUSTING ANY COMPARISON:
The validating backtest (ORB15_SPEC.md) ran on Polygon SIP bars:
the consolidated tape, every exchange, deterministic. This
runner's live bar source is Alpaca's FREE real-time feed
(``feed=iex``): IEX only, roughly 3% of consolidated volume.
IEX-only 1-minute bars can differ from SIP bars in their highs,
lows and closes, so the runner may compute a slightly different
opening range and see different breakout closes than the backtest
tape showed for the same day. Neither system is buggy: the
forward test measures ORB-15 AS IT IS ACTUALLY DEPLOYABLE FOR
FREE, signal-feed divergence included, and if the forward test
disappoints, feed divergence is a named suspect. The bar source
is an injectable interface (see BarSource) precisely so a
Polygon-SIP source can replace the IEX source later without
touching strategy or runner logic.

STATUS: deployed 2026-10-21 on the Linux box
(orb15-alpaca-paper.service, orders enabled the same day). The
first start exposed the live-start catch-up bug fixed by the
live-frontier gate described above; 2026-10-21 is a deploy-day
artifact excluded from scoring — the scored forward test starts
Thu 2026-10-22 at exactly $500.00. See the README section
"ORB-15 Alpaca paper forward test" and
deploy/INSTALL_orb15_alpaca.md for the service setup.

HARDENING (2026-10-21, after docs/alpaca_runner_audit.md):
deterministic client_order_ids with lookup recovery and an
orphan-flatten policy (F1); hard entry caps MAX_SHARES /
MAX_NOTIONAL_USD with size_capped events (F5); cancel_order +
partially_filled as a live state, partial-exit remainders kept
under level management (F2/F4); an exit chain that can never
kill the process — market order, close_position fallback,
exit_failure_critical + per-bar retry (F3); day-start market
calendar with early-close flatten times and a loud fallback
(F6); a single-instance flock (F7); corrupt-state quarantine
into a halted, reconciled start (F8); a wall-clock flatten
backstop watchdog in serve() (F11); redirect-refusing HTTP
transport (F13, in client.py). F10/F14/F9/F12/F15 remain open
by design — they alter strategy semantics.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time as time_module

try:
    import fcntl
except ImportError:  # pragma: no cover - the service runs on Linux
    fcntl = None  # type: ignore[assignment]
from dataclasses import asdict, dataclass
from datetime import date as date_type
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Protocol
from zoneinfo import ZoneInfo

from fcr.patterns import Bar
from fcr.strategies_intraday import (
    ENTRY_CUTOFF,
    FLATTEN_TIME,
    SESSION_END,
    SESSION_START,
    STRATEGY_FACTORIES,
    SymbolMeta,
)

from .client import AlpacaAPIError, AlpacaClient
from .config import (
    ENV_STATE_DIR,
    AlpacaConfig,
    MissingCredentialsError,
    PaperOnlyError,
    runner_enabled,
)

ET = ZoneInfo("America/New_York")

SYMBOL = "SPY"
OVERLAY_START_USD = 500.0
FRICTION_PER_SHARE_USD = 0.04  # the backtest's AF model, for comparison

# Hard entry-size caps (hardening F5) — the ONE config place for
# them. The frozen strategy sizes floor($150 / stop distance) with
# no ceiling; on the real tape that reached 300 shares / ~$182k
# notional against a $500 overlay. An entry order is now
# min(spec shares, MAX_SHARES, floor(MAX_NOTIONAL_USD / entry
# price)); when a cap binds, the trade is booked at its actual
# size and a size_capped event records spec vs actual.
MAX_SHARES = 200
MAX_NOTIONAL_USD = 150_000.0

PHASE_IDLE = "IDLE"
PHASE_RANGE_BUILDING = "RANGE_BUILDING"
PHASE_ARMED = "ARMED"
PHASE_IN_POSITION = "IN_POSITION"
PHASE_DONE = "DONE"
PHASE_HALTED = "HALTED"

FEED_NOTE = (
    "Live signals come from Alpaca's free IEX real-time feed "
    "(~3% of consolidated volume); the validating backtest used "
    "Polygon SIP (consolidated) bars. The forward test measures "
    "ORB-15 as deployable for free, feed divergence included. "
    "The BarSource interface lets a Polygon-SIP source replace "
    "the IEX source without touching strategy logic."
)

# The frozen SPY meta, identical to run_strategies_intraday.SYMBOL_META.
SPY_META = SymbolMeta(
    symbol="SPY",
    point_value_usd=1.0,
    tick_size=0.01,
    max_units=None,
    friction_per_unit_usd=FRICTION_PER_SHARE_USD,
    risk_budget_usd=150.0,
    instrument_type="equity",
)


class ReconciliationHalt(RuntimeError):
    """State file and paper account disagree in a way the runner
    cannot fully explain. The runner halts and places no orders."""


class OrderError(AlpacaAPIError):
    """An order failed, was rejected/canceled, or did not fill."""


# ----------------------------------------------------------------------
# Bars
# ----------------------------------------------------------------------
class BarSource(Protocol):
    """Injectable bar source: completed RTH 1-minute bars for one ET
    day, ascending. Swap implementations (IEX live, CSV replay, a
    future Polygon-SIP source) without touching the runner."""

    def bars_for_day(self, day: date_type) -> Iterator[Bar]:
        ...


def _is_rth(bar: Bar) -> bool:
    return SESSION_START <= bar.ts.time() < SESSION_END


class CsvBarSource:
    """BarSource over a loaded bars DataFrame (fcr.data.load_csv
    shape) — replay and tests."""

    def __init__(self, df):
        self._df = df

    @classmethod
    def from_path(cls, path) -> "CsvBarSource":
        from fcr.data import load_csv

        return cls(load_csv(path))

    def bars_for_day(self, day: date_type) -> Iterator[Bar]:
        df = self._df
        day_df = df[df["timestamp"].dt.date == day]
        for ts, o, h, l, c, v in zip(
            day_df["timestamp"],
            day_df["open"],
            day_df["high"],
            day_df["low"],
            day_df["close"],
            day_df["volume"],
        ):
            bar = Bar(
                ts=ts.to_pydatetime(),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
            )
            if _is_rth(bar):
                yield bar


class AlpacaBarSource:
    """Live BarSource: polls the Alpaca data API (free IEX feed) and
    yields each 1-minute bar once it is COMPLETE (bar start + 60s
    <= now). Clock and sleeper are injectable for tests. On a past
    day it yields the whole day at once; during the session it polls
    until 16:00 ET. A market holiday simply yields nothing."""

    def __init__(
        self,
        client: AlpacaClient,
        clock: Callable[[], datetime],
        sleep_fn: Callable[[float], None] = time_module.sleep,
        poll_seconds: float = 20.0,
        symbol: str = SYMBOL,
        feed: str = "iex",
    ):
        self._client = client
        self._clock = clock
        self._sleep = sleep_fn
        self._poll_seconds = poll_seconds
        self.symbol = symbol
        self.feed = feed

    def bars_for_day(self, day: date_type) -> Iterator[Bar]:
        start_dt = datetime.combine(day, SESSION_START, tzinfo=ET)
        end_dt = datetime.combine(day, SESSION_END, tzinfo=ET)
        seen: set[datetime] = set()
        while True:
            now = self._clock()
            fetched = self._client.get_bars(
                self.symbol, start_dt, end_dt, feed=self.feed
            )
            for bar in fetched:
                if bar.ts.date() != day or not _is_rth(bar):
                    continue
                complete = bar.ts + timedelta(minutes=1) <= now
                if complete and bar.ts not in seen:
                    seen.add(bar.ts)
                    yield bar
            if now.date() > day:
                return
            if now.date() == day and now.time() >= SESSION_END:
                return
            if now.date() < day:
                return
            self._sleep(self._poll_seconds)


# ----------------------------------------------------------------------
# Brokers
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Fill:
    price: float
    qty: int  # shares actually filled (positive)
    order_id: Optional[str] = None


class Broker(Protocol):
    """What the runner needs from a broker: signed position,
    market-order fills, and a whole-position close. AlpacaBroker is
    production; SimulatedBroker is replay/test machinery.

    A broker MAY also expose ``set_client_order_id(cid)``: the
    runner calls it (when present) immediately before place_market
    so the order carries a deterministic client_order_id
    (hardening F1). Brokers without it are called exactly as
    before."""

    def position_qty(self, symbol: str) -> int:
        ...

    def place_market(self, symbol: str, qty: int, side: str) -> Fill:
        ...

    def close_position(self, symbol: str) -> Fill:
        ...


class AlpacaBroker:
    """Broker over AlpacaClient (PAPER). Places a market order and
    polls it to a fill; a partially-filled order that reaches a final
    state fills for the filled quantity only — never assumed.

    Hardening (2026-10-21 audit):
    * F1 — when the runner supplies a client_order_id and the POST
      itself fails with an UNKNOWN outcome (transport error, or a
      409 duplicate-id conflict), the order is looked up by that id
      before anything is concluded. A definitive refusal (any other
      HTTP error — Alpaca answered, so nothing was created) raises
      immediately.
    * F2 — ``partially_filled`` is a LIVE state: polling continues
      until the order is terminal or the poll deadline passes; at
      the deadline the unfilled remainder is canceled and the
      filled quantity is booked."""

    _FINAL_BAD = {"canceled", "expired", "rejected", "suspended"}

    def __init__(
        self,
        client: AlpacaClient,
        sleep_fn: Callable[[float], None] = time_module.sleep,
        poll_seconds: float = 1.0,
        max_polls: int = 30,
    ):
        self._client = client
        self._sleep = sleep_fn
        self._poll_seconds = poll_seconds
        self._max_polls = max_polls
        self._pending_client_order_id: Optional[str] = None

    def set_client_order_id(self, client_order_id: Optional[str]) -> None:
        """One-shot: the id the NEXT place_market submission carries."""
        self._pending_client_order_id = client_order_id

    def position_qty(self, symbol: str) -> int:
        pos = self._client.get_position(symbol)
        if pos is None:
            return 0
        return int(float(pos.get("qty") or 0))

    def place_market(self, symbol: str, qty: int, side: str) -> Fill:
        client_order_id, self._pending_client_order_id = (
            self._pending_client_order_id,
            None,
        )
        try:
            if client_order_id is not None:
                order = self._client.place_market_order(
                    symbol, qty, side, client_order_id=client_order_id
                )
            else:
                order = self._client.place_market_order(symbol, qty, side)
        except AlpacaAPIError as exc:
            order = self._recover_submission(client_order_id, exc)
        return self._await_fill(order["id"])

    def close_position(self, symbol: str) -> Fill:
        """Market-close the whole position (DELETE /v2/positions)
        and poll the closing order to its fill."""
        order = self._client.close_position(symbol)
        return self._await_fill(order["id"])

    # ------------------------------------------------------------ internals
    def _recover_submission(self, client_order_id: Optional[str], exc) -> dict:
        """A place_market_order POST raised. Decide what is KNOWN:
        a definitive HTTP refusal means Alpaca answered and created
        nothing; a transport failure or a 409 means the outcome is
        unknown — look the order up by client_order_id (F1)."""
        if client_order_id is None:
            raise exc
        if exc.status is not None and exc.status != 409:
            raise OrderError(
                f"Alpaca refused the order submission (HTTP "
                f"{exc.status}): {exc}",
                status=exc.status,
                payload=exc.payload,
            ) from exc
        for attempt in range(3):
            try:
                found = self._client.get_order_by_client_order_id(
                    client_order_id
                )
                if found.get("id"):
                    return found
            except AlpacaAPIError as lookup_exc:
                if lookup_exc.status != 404:
                    raise OrderError(
                        f"order submission outcome unknown and the "
                        f"client_order_id lookup itself failed: "
                        f"{lookup_exc}",
                        status=lookup_exc.status,
                        payload=lookup_exc.payload,
                    ) from lookup_exc
            if attempt < 2:
                self._sleep(self._poll_seconds)
        raise OrderError(
            f"order submission outcome unknown and no order exists "
            f"under client_order_id {client_order_id!r}; if the "
            f"order did fill, day-start reconciliation's orphan "
            f"policy will flatten it"
        ) from exc

    def _cancel_remainder(self, order_id: str) -> None:
        """Best-effort cancel of an order's unfilled remainder (F2).
        Duck-typed: a minimal client without cancel_order simply
        skips this (the poll deadline still bounds the wait)."""
        cancel = getattr(self._client, "cancel_order", None)
        if cancel is None:
            return
        try:
            cancel(order_id)
        except Exception:
            pass  # the fill we already have is booked either way

    def _await_fill(self, order_id: str) -> Fill:
        last_partial: Optional[dict] = None
        for _ in range(self._max_polls):
            current = self._client.get_order(order_id)
            status = str(current.get("status") or "")
            filled_qty = int(float(current.get("filled_qty") or 0))
            if status == "filled" or (
                status in self._FINAL_BAD and filled_qty > 0
            ):
                price = current.get("filled_avg_price")
                if price is None:
                    raise OrderError(
                        f"Alpaca order {order_id} reports filled_qty="
                        f"{filled_qty} but no filled_avg_price.",
                        payload=current,
                    )
                return Fill(price=float(price), qty=filled_qty, order_id=order_id)
            if status in self._FINAL_BAD:
                raise OrderError(
                    f"Alpaca order {order_id} ended {status} with no fill.",
                    payload=current,
                )
            if status == "partially_filled" and filled_qty > 0:
                # F2: a live state, not a failure — keep polling
                # until terminal or the deadline, remembering the
                # best-known partial fill.
                last_partial = current
            self._sleep(self._poll_seconds)
        # Deadline: cancel whatever is unfilled, then book exactly
        # what filled (F2) — or refuse, if nothing did.
        self._cancel_remainder(order_id)
        if last_partial is not None:
            filled_qty = int(float(last_partial.get("filled_qty") or 0))
            price = last_partial.get("filled_avg_price")
            if filled_qty > 0 and price is not None:
                return Fill(
                    price=float(price), qty=filled_qty, order_id=order_id
                )
        raise OrderError(
            f"Alpaca order {order_id} did not fill after "
            f"{self._max_polls} polls (remainder cancel requested); "
            f"refusing to assume a fill."
        )


class SimulatedBroker:
    """Fill-at-market-price broker for replay and tests. The
    "market price" is whatever was last set with set_price (the
    replay driver sets it to each bar's close as bars stream, so an
    order placed on a bar's signal fills at that bar's close —
    the closest offline analogue of a live market fill)."""

    def __init__(self, price: float = 0.0):
        self._price = float(price)
        self._qty = 0
        self.orders: list[dict] = []
        self._seq = 0

    def set_price(self, price: float) -> None:
        self._price = float(price)

    def seed_position(self, qty_signed: int) -> None:
        """Pre-load a position (restart/reconciliation tests)."""
        self._qty = int(qty_signed)

    def position_qty(self, symbol: str) -> int:
        return self._qty

    def place_market(self, symbol: str, qty: int, side: str) -> Fill:
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")
        self._seq += 1
        order_id = f"sim-{self._seq}"
        self.orders.append(
            {"id": order_id, "symbol": symbol, "qty": qty, "side": side,
             "price": self._price}
        )
        self._qty += qty if side == "buy" else -qty
        return Fill(price=self._price, qty=qty, order_id=order_id)

    def close_position(self, symbol: str) -> Fill:
        """Close the whole position at the current market price.

        Deliberately NOT routed through place_market: on the real
        API this is a different endpoint (DELETE /v2/positions),
        and the runner's F3 exit fallback depends on it working
        when plain exit orders are being rejected."""
        if self._qty == 0:
            raise OrderError(f"no {symbol} position to close")
        qty = abs(self._qty)
        side = "sell" if self._qty > 0 else "buy"
        self._seq += 1
        order_id = f"sim-close-{self._seq}"
        self.orders.append(
            {"id": order_id, "symbol": symbol, "qty": qty, "side": side,
             "price": self._price, "kind": "close_position"}
        )
        self._qty = 0
        return Fill(price=self._price, qty=qty, order_id=order_id)


def priced_bars(bars: Iterable[Bar], broker: SimulatedBroker) -> Iterator[Bar]:
    """Yield bars while keeping a SimulatedBroker's market price at
    each bar's close (replay/test wiring)."""
    for bar in bars:
        broker.set_price(bar.close)
        yield bar


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------
class EventLog:
    """Append-only JSONL event log (+ optional in-memory sink)."""

    def __init__(self, path: Optional[Path] = None, sink: Optional[list] = None):
        self.path = Path(path) if path is not None else None
        self.sink = sink
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, ts: Optional[datetime] = None, **fields) -> dict:
        record = {
            "ts": (ts or datetime.now(ET)).isoformat(),
            "type": event_type,
            **fields,
        }
        if self.sink is not None:
            self.sink.append(record)
        if self.path is not None:
            with self.path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
        return record


class StateStore:
    """The runner's small JSON state file, rewritten atomically
    (tmp + rename) after every transition. path=None keeps it in
    memory (tests)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else None
        self._memory: Optional[dict] = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Optional[dict]:
        if self.path is None:
            return json.loads(json.dumps(self._memory)) if self._memory else None
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text())

    def save(self, state: dict) -> None:
        if self.path is None:
            self._memory = json.loads(json.dumps(state))
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, self.path)


class InstanceLock:
    """Single-instance guard (hardening F7): a non-blocking flock
    on a lock file in the state dir. The service is the only runner;
    a second process (e.g. a manual ``python -m alpaca.runner``
    during debugging) must refuse to start rather than double-trade
    the account. acquire() returns False when another live process
    holds the lock — the caller logs the refusal and exits CLEANLY
    (no crash loop). The fd is held for the process lifetime; the
    OS releases the lock if the holder dies."""

    def __init__(self, path):
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        if fcntl is None:  # pragma: no cover - non-Unix fallback
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None and fcntl is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------
@dataclass
class RunnerPosition:
    direction: str  # 'long' | 'short'
    qty: int        # shares held (positive)
    entry_price: float          # actual fill
    strategy_entry_price: float # the spec's bar-close entry
    entry_time: str             # HH:MM ET of the signal bar
    stop: float
    target: float
    entry_order_id: Optional[str] = None

    @property
    def signed_qty(self) -> int:
        return self.qty if self.direction == "long" else -self.qty

    def to_state(self) -> dict:
        return asdict(self)

    @classmethod
    def from_state(cls, raw: dict) -> "RunnerPosition":
        return cls(**raw)


def _entry_side(direction: str) -> str:
    return "buy" if direction == "long" else "sell"


def _exit_side(direction: str) -> str:
    return "sell" if direction == "long" else "buy"


class Orb15Runner:
    """The ORB-15 forward-test engine (see module docstring)."""

    def __init__(
        self,
        broker: Broker,
        *,
        enabled: bool = False,
        log: Optional[EventLog] = None,
        store: Optional[StateStore] = None,
        clock: Optional[Callable[[], datetime]] = None,
        symbol: str = SYMBOL,
        overlay_start_usd: float = OVERLAY_START_USD,
        friction_per_share_usd: float = FRICTION_PER_SHARE_USD,
        max_shares: int = MAX_SHARES,
        max_notional_usd: float = MAX_NOTIONAL_USD,
        calendar_fn: Optional[Callable] = None,
    ):
        self.broker = broker
        self.enabled = enabled
        self.log = log or EventLog()
        self.store = store or StateStore()
        self._clock = clock or (lambda: datetime.now(ET))
        self.symbol = symbol
        self.overlay_start_usd = float(overlay_start_usd)
        self.friction_per_share_usd = float(friction_per_share_usd)
        # F5 hard caps (defaults from the module constants — the
        # one config place; injectable only for tests).
        self.max_shares = int(max_shares)
        self.max_notional_usd = float(max_notional_usd)
        # F6 market calendar: callable(day) -> {"open": time,
        # "close": time} for a trading day, None for a non-trading
        # day; raising means "calendar unavailable" (fall back to
        # the default schedule, loudly). None = not configured:
        # default RTH schedule assumed.
        self.calendar_fn = calendar_fn
        self._pos: Optional[RunnerPosition] = None
        self._phase = PHASE_IDLE
        # The current day run's live frontier (see run_day). None =
        # no frontier: every bar counts as live (offline replay).
        self._live_since: Optional[datetime] = None
        # Per-run hardening bookkeeping (reset every run_day):
        self._current_day: Optional[date_type] = None
        self._exit_seq = 0
        self._exit_failures = 0
        self._exit_pending_reason: Optional[str] = None
        self._exit_pending_trade = None
        self._remainder_managed = False
        self._remainder_bar_ts: Optional[datetime] = None
        self._flatten_time: time = FLATTEN_TIME
        self._day_flatten_dt: Optional[datetime] = None
        self._day_state: Optional[dict] = None
        self._day_summary: Optional[dict] = None
        self._last_bar_at: Optional[datetime] = None

    # ------------------------------------------------------------ state io
    def _fresh_state(self, day: date_type) -> dict:
        return {
            "version": 1,
            "date": day.isoformat(),
            "phase": PHASE_IDLE,
            "position": None,
            "overlay_equity_usd": round(self.overlay_start_usd, 2),
            "realized_pnl_total_usd": 0.0,
            "halted": False,
            "halt_reason": None,
        }

    def _save(self, state: dict) -> None:
        state["phase"] = self._phase
        state["position"] = self._pos.to_state() if self._pos else None
        self.store.save(state)

    # -------------------------------------------------------- reconciliation
    def _reconcile(self, day: date_type, state: dict) -> None:
        """Compare state vs the broker's actual SPY position.

        Raises ReconciliationHalt on any unexplained disagreement.
        Handles the overnight-leftover case (a position from a prior
        date that matches its state record exactly) by flattening it
        and booking the result — that position is fully understood,
        so closing it is not a guess."""
        broker_qty = self.broker.position_qty(self.symbol)
        raw_pos = state.get("position")
        state_qty = 0
        if raw_pos:
            pos = RunnerPosition.from_state(raw_pos)
            state_qty = pos.signed_qty
        state_date = state.get("date")

        if raw_pos and state_date and state_date < day.isoformat():
            # Overnight leftover: yesterday's position never flattened
            # (the process must have died). Only proceed if the broker
            # agrees EXACTLY; then flatten it now and book the pnl.
            if broker_qty != state_qty:
                raise ReconciliationHalt(
                    f"overnight leftover mismatch: state holds "
                    f"{state_qty} {self.symbol} from {state_date} but "
                    f"the paper account holds {broker_qty}"
                )
            pos = RunnerPosition.from_state(raw_pos)
            self._pos = pos
            self.log.emit(
                "overnight_leftover_detected",
                ts=self._clock(),
                date=day.isoformat(),
                position=pos.to_state(),
            )
            # Exit through the fallback chain (F3): a rejected
            # market order falls back to close_position; only if
            # BOTH fail does the leftover halt the runner.
            self._set_broker_cid(self._client_order_id("leftover"))
            try:
                fill = self.broker.place_market(
                    self.symbol, pos.qty, _exit_side(pos.direction)
                )
            except Exception:
                try:
                    fill = self.broker.close_position(self.symbol)
                except Exception as exc2:
                    raise ReconciliationHalt(
                        f"overnight leftover flatten failed via both "
                        f"market order and close_position: {exc2}"
                    ) from exc2
            pnl = self._book_exit(fill)
            state["realized_pnl_total_usd"] = round(
                float(state.get("realized_pnl_total_usd") or 0.0) + pnl, 2
            )
            self.log.emit(
                "overnight_leftover_flattened",
                ts=self._clock(),
                date=day.isoformat(),
                fill_price=fill.price,
                pnl_usd=pnl,
            )
            state["position"] = None
            return

        if state_date and state_date > day.isoformat():
            raise ReconciliationHalt(
                f"state file date {state_date} is after today "
                f"{day.isoformat()} — clock or state corruption"
            )
        if broker_qty != 0 and state_qty == 0:
            # ORPHAN POLICY (hardening F1): a holding the state
            # file cannot explain is never left riding unmanaged
            # and never adopted on a guess — it is FLATTENED via
            # close_position, logged in full, and entries halt for
            # the day. (Classic cause: an entry fill the runner
            # never saw because the submission's outcome was lost.)
            try:
                self._flatten_orphan_position(
                    day, state, broker_qty,
                    origin="reconciliation_unknown_holding",
                )
            except Exception as exc:
                raise ReconciliationHalt(
                    f"paper account holds {broker_qty} {self.symbol} "
                    f"the runner state cannot explain, and the "
                    f"orphan flatten failed: {exc}"
                ) from exc
            raise ReconciliationHalt(
                f"paper account held {broker_qty} {self.symbol} the "
                "runner state could not explain — flattened under "
                "the orphan policy (see the orphan_flattened "
                "event); entries halted for the day"
            )
        if state_qty != 0 and broker_qty == 0:
            raise ReconciliationHalt(
                f"runner state claims {state_qty} {self.symbol} but "
                "the paper account is flat — the position vanished "
                "outside the runner"
            )
        if broker_qty != state_qty:
            raise ReconciliationHalt(
                f"position quantity mismatch: state {state_qty} vs "
                f"paper account {broker_qty} {self.symbol}"
            )
        if raw_pos:
            self._pos = RunnerPosition.from_state(raw_pos)
            self.log.emit(
                "reconciliation_ok",
                ts=self._clock(),
                date=day.isoformat(),
                resumed_position=self._pos.to_state(),
            )
        else:
            self.log.emit(
                "reconciliation_ok", ts=self._clock(), date=day.isoformat()
            )

    # ------------------------------------------------------------- booking
    def _book_exit(self, fill: Fill) -> float:
        """Book a closing fill against the open position: overlay
        pnl from ACTUAL fills. Returns pnl. A partial exit fill
        leaves the remainder open at the same entry price."""
        pos = self._pos
        assert pos is not None
        sign = 1.0 if pos.direction == "long" else -1.0
        pnl = sign * (fill.price - pos.entry_price) * fill.qty
        # A partial exit fill leaves the remainder open.
        if fill.qty < pos.qty:
            self._pos = RunnerPosition(
                direction=pos.direction,
                qty=pos.qty - fill.qty,
                entry_price=pos.entry_price,
                strategy_entry_price=pos.strategy_entry_price,
                entry_time=pos.entry_time,
                stop=pos.stop,
                target=pos.target,
                entry_order_id=pos.entry_order_id,
            )
        else:
            self._pos = None
        return round(pnl, 2)

    # -------------------------------------------- hardening helpers
    def _client_order_id(self, purpose: str) -> str:
        """Deterministic client_order_id (F1): per date + purpose +
        exit sequence. A given day's entry is ALWAYS
        ``orb15-<date>-entry`` — identical across process restarts,
        which is what makes a lost submission recoverable by
        lookup instead of guessable."""
        day = self._current_day.isoformat() if self._current_day else "nodate"
        if purpose == "exit":
            return f"orb15-{day}-exit-{self._exit_seq}"
        return f"orb15-{day}-{purpose}"

    def _set_broker_cid(self, client_order_id: Optional[str]) -> None:
        """Hand the broker the id for its next submission, when the
        broker supports one (see the Broker protocol note)."""
        setter = getattr(self.broker, "set_client_order_id", None)
        if callable(setter):
            setter(client_order_id)

    def _capped_qty(self, units: int, price: float):
        """Apply the F5 hard caps. Returns (qty, info-or-None);
        info describes the clipping when a cap binds."""
        by_notional = units
        if price and price > 0:
            by_notional = int(
                math.floor(self.max_notional_usd / float(price))
            )
        qty = min(int(units), self.max_shares, by_notional)
        if qty >= units:
            return int(units), None
        binding = []
        if self.max_shares <= by_notional:
            binding.append("max_shares")
        if by_notional <= self.max_shares:
            binding.append("max_notional")
        info = {
            "spec_shares": int(units),
            "capped_shares": int(qty),
            "strategy_entry_price": float(price),
            "spec_notional_usd": round(int(units) * float(price), 2),
            "capped_notional_usd": round(int(qty) * float(price), 2),
            "binding_caps": binding,
            "max_shares": self.max_shares,
            "max_notional_usd": self.max_notional_usd,
        }
        return int(qty), info

    def _exit_fill(
        self,
        qty: int,
        side: str,
        *,
        context: str,
        ts: Optional[datetime] = None,
    ) -> Optional[Fill]:
        """THE exit-order path (hardening F3): an exit failure must
        never kill the process or die silently. Try a market order;
        on ANY failure fall back to close_position ONCE; if that
        fails too, log exit_failure_critical and return None — the
        caller keeps the position under management and retries on
        subsequent bars. (Entries deliberately do NOT come through
        here: they may still fail the day, per the audit scope.)"""
        when = ts or self._clock()
        day = self._current_day.isoformat() if self._current_day else None
        self._exit_seq += 1
        self._set_broker_cid(self._client_order_id("exit"))
        try:
            return self.broker.place_market(self.symbol, qty, side)
        except Exception as exc:
            self.log.emit(
                "exit_order_failed",
                ts=when,
                date=day,
                context=context,
                qty=qty,
                side=side,
                error=f"{type(exc).__name__}: {exc}",
                fallback="close_position",
            )
        try:
            return self.broker.close_position(self.symbol)
        except Exception as exc2:
            self._exit_failures += 1
            self.log.emit(
                "exit_failure_critical",
                ts=when,
                date=day,
                context=context,
                qty=qty,
                side=side,
                error=f"{type(exc2).__name__}: {exc2}",
                note=(
                    "exit failed via market order AND close_position; "
                    "position remains under management and the exit "
                    "will be retried on subsequent bars / at flatten"
                ),
            )
            return None

    def _flatten_orphan_position(
        self, day: date_type, state: dict, broker_qty: int, *, origin: str
    ) -> Fill:
        """ORPHAN POLICY (F1): flatten an account holding the state
        file cannot explain, via close_position, with the full
        detail logged. The orphan's entry fill is unknown to the
        runner, so NO overlay pnl is booked for it — the overlay
        tracks strategy trades only; the event says so explicitly.
        Raises on failure (the caller decides halt vs log)."""
        self.log.emit(
            "orphan_detected",
            ts=self._clock(),
            date=day.isoformat(),
            broker_qty=broker_qty,
            state_position=state.get("position"),
            origin=origin,
        )
        fill = self.broker.close_position(self.symbol)
        remaining = self.broker.position_qty(self.symbol)
        if remaining != 0:
            raise OrderError(
                f"orphan flatten incomplete: {remaining} "
                f"{self.symbol} still held after close_position"
            )
        self.log.emit(
            "orphan_flattened",
            ts=self._clock(),
            date=day.isoformat(),
            flattened_qty=broker_qty,
            fill_price=fill.price,
            order_id=fill.order_id,
            origin=origin,
            pnl_booked_usd=0.0,
            note=(
                "entry fill unknown to the runner (the position was "
                "never in its state); overlay not adjusted"
            ),
        )
        return fill

    def _quarantine_state(self, day: date_type, exc: Exception) -> dict:
        """F8: a corrupt state file is quarantined (renamed aside,
        never deleted), logged loudly, and replaced with a fresh
        HALTED state pending reconciliation — instead of the old
        behavior, an uncaught JSONDecodeError crash-looping the
        service every 30s."""
        quarantined_to = None
        path = self.store.path
        if path is not None and path.exists():
            stamp = self._clock().strftime("%Y%m%dT%H%M%S")
            target = path.with_name(f"runner_state.corrupt-{stamp}.json")
            os.replace(path, target)
            quarantined_to = str(target)
        self.log.emit(
            "state_quarantined",
            ts=self._clock(),
            date=day.isoformat(),
            error=f"{type(exc).__name__}: {exc}",
            quarantined_to=quarantined_to,
            note=(
                "state file was corrupt; starting HALTED pending "
                "reconciliation (flat account -> stay halted; "
                "holding -> orphan policy flattens it)"
            ),
        )
        state = self._fresh_state(day)
        state["halted"] = True
        state["halt_reason"] = (
            f"state file corrupt ({type(exc).__name__}); quarantined "
            f"to {quarantined_to}; starting halted pending "
            "reconciliation"
        )
        state["state_quarantined"] = True
        state["quarantine_reconciled"] = False
        return state

    def _reconcile_quarantine(self, day: date_type, state: dict) -> None:
        """The F8 follow-through, run once from the halted branch:
        check the account for the quarantined start. Flat -> stay
        halted (a human reviews). Holding -> the F1 orphan path
        flattens it; a flatten failure is logged critical and the
        runner STAYS halted (loud, no crash loop)."""
        try:
            qty = self.broker.position_qty(self.symbol)
            if qty == 0:
                self.log.emit(
                    "quarantine_reconciled_flat",
                    ts=self._clock(),
                    date=day.isoformat(),
                    note="account is flat; runner stays halted for review",
                )
            else:
                self._flatten_orphan_position(
                    day, state, qty, origin="corrupt_state_quarantine"
                )
                self.log.emit(
                    "quarantine_orphan_flattened",
                    ts=self._clock(),
                    date=day.isoformat(),
                    broker_qty=qty,
                )
        except Exception as exc:
            self.log.emit(
                "quarantine_reconcile_failed",
                ts=self._clock(),
                date=day.isoformat(),
                error=f"{type(exc).__name__}: {exc}",
                note="runner stays halted; manual intervention required",
            )
        state["quarantine_reconciled"] = True
        self.store.save(state)

    def _resolve_schedule(self, day: date_type) -> dict:
        """F6: resolve the day's market schedule via calendar_fn.
        Returns {status, market_open, market_close, non_trading,
        flatten_time}; also sets self._flatten_time and
        self._day_flatten_dt. A calendar failure falls back to the
        default RTH schedule and logs calendar_unavailable —
        never silent."""
        schedule = {
            "status": "not_configured",
            "market_open": None,
            "market_close": None,
            "non_trading": False,
            "flatten_time": FLATTEN_TIME,
        }
        if self.calendar_fn is not None:
            try:
                entry = self.calendar_fn(day)
            except Exception as exc:
                schedule["status"] = "unavailable_fallback"
                self.log.emit(
                    "calendar_unavailable",
                    ts=self._clock(),
                    date=day.isoformat(),
                    error=f"{type(exc).__name__}: {exc}",
                    note="falling back to the default RTH schedule",
                )
            else:
                if entry is None:
                    schedule["status"] = "ok"
                    schedule["non_trading"] = True
                else:
                    schedule["status"] = "ok"
                    schedule["market_open"] = entry.get("open")
                    schedule["market_close"] = entry.get("close")
                    close = entry.get("close")
                    if close is not None and close < time(16, 0):
                        close_dt = datetime.combine(day, close, tzinfo=ET)
                        early = (close_dt - timedelta(minutes=5)).time()
                        schedule["flatten_time"] = min(FLATTEN_TIME, early)
        self._flatten_time = schedule["flatten_time"]
        self._day_flatten_dt = datetime.combine(
            day, self._flatten_time, tzinfo=ET
        )
        return schedule

    def _maybe_backstop_flatten(self, now: datetime) -> bool:
        """F11 wall-clock flatten backstop (contained version):
        if a position is open, the day's flatten time has passed,
        and no bar has been processed for over 3 minutes (the feed
        has stalled — bars can no longer drive the exit), attempt
        the flatten through the normal exit path. Returns True when
        the backstop fired (whether or not the exit succeeded — a
        failure is logged exit_failure_critical by _exit_fill and
        the next poll retries). Called by serve()'s watchdog
        thread; also directly testable."""
        if self._pos is None or self._day_state is None:
            return False
        if self._day_flatten_dt is None or now < self._day_flatten_dt:
            return False
        if self._last_bar_at is None:
            return False
        if now - self._last_bar_at <= timedelta(minutes=3):
            return False
        state = self._day_state
        summary = self._day_summary
        pos = self._pos
        self.log.emit(
            "flatten_backstop_fired",
            ts=now,
            date=state.get("date"),
            direction=pos.direction,
            qty=pos.qty,
            flatten_time=self._flatten_time.strftime("%H:%M"),
            last_bar_at=self._last_bar_at.isoformat(),
            note="no bar processed for over 3 minutes past the "
                 "flatten time; exiting via the normal exit path",
        )
        fill = self._exit_fill(
            pos.qty, _exit_side(pos.direction),
            context="wall_clock_backstop", ts=now,
        )
        if fill is None:
            return True
        pnl = self._book_exit(fill)
        if summary is not None:
            self._apply_pnl(state, summary, pnl, fill.qty)
            summary["trades"] += 1
        self.log.emit(
            "trade_closed",
            ts=now,
            date=state.get("date"),
            direction=pos.direction,
            exit_reason="EOD_FLATTEN",
            strategy_exit_price=None,
            fill_exit_price=fill.price,
            exit_order_id=fill.order_id,
            managed_by="wall_clock_backstop",
            pnl_usd=pnl,
            overlay_equity_usd=state.get("overlay_equity_usd"),
        )
        self._remainder_managed = False
        self._remainder_bar_ts = None
        self._exit_pending_reason = None
        self._exit_pending_trade = None
        if self._pos is None:
            self._phase = PHASE_DONE
        self._save(state)
        return True

    # ------------------------------------------------- live-frontier gate
    def _bar_is_live(self, bar: Bar) -> bool:
        """A bar is LIVE iff it completed after this session's live
        frontier — the runner saw it happen. A 1-minute bar completes
        at ts + 1 minute; anything already complete when consumption
        began (``live_since``) is start-of-day catch-up history. No
        frontier (offline replay / unit tests) = every bar is live."""
        if self._live_since is None:
            return True
        return bar.ts + timedelta(minutes=1) > self._live_since

    def _place_entry_order(self, bar: Bar, qty: int, side: str) -> Fill:
        """THE entry-order path — the live-frontier gate lives here
        so no caller can bypass it: an entry order is placed only
        for a signal on a LIVE bar. Exits never come through here —
        they manage real positions (possibly adopted at restart,
        decided on historical bars) and are never gated."""
        if not self._bar_is_live(bar):
            raise RuntimeError(
                "live-frontier gate: refusing to place an entry "
                f"order for historical bar {bar.ts.isoformat()} "
                f"(live_since={self._live_since})"
            )
        # F1: every entry carries the deterministic per-day
        # client_order_id, so a lost submission outcome is
        # recoverable by lookup (and a duplicate POST is a 409 the
        # broker resolves by lookup — never a second position).
        self._set_broker_cid(self._client_order_id("entry"))
        return self.broker.place_market(self.symbol, qty, side)

    # ----------------------------------------------------------------- day
    def run_day(
        self,
        day: date_type,
        bars: Iterable[Bar],
        *,
        live_since: Optional[datetime] = None,
    ) -> dict:
        """Run one ET trading day over an iterable of completed RTH
        1-minute bars. Returns the day summary (also logged).

        ``live_since`` is the live frontier: the moment the runner
        began consuming this day live. Bars already complete at
        that moment are start-of-day catch-up — replayed through
        the strategy (its state must be rebuilt from them) but
        never allowed to place an entry order. None = no frontier:
        every bar counts as live (offline replay / unit tests)."""
        # F8: a corrupt state file must not crash-loop the service
        # — quarantine it and start halted pending reconciliation.
        try:
            state = self.store.load()
        except json.JSONDecodeError as exc:
            state = self._quarantine_state(day, exc)
        state = state or self._fresh_state(day)
        if state.get("date") != day.isoformat() and not state.get("position"):
            # New day, flat: roll the date, keep the overlay equity.
            state["date"] = day.isoformat()
        self._pos = None
        self._phase = PHASE_IDLE
        # Per-run hardening bookkeeping.
        self._current_day = day
        self._exit_seq = 0
        self._exit_failures = 0
        self._exit_pending_reason = None
        self._exit_pending_trade = None
        self._remainder_managed = False
        self._remainder_bar_ts = None
        self._day_state = None
        self._day_summary = None
        self._last_bar_at = None
        summary = {
            "date": day.isoformat(),
            "halted": False,
            "trades": 0,
            "realized_pnl_usd": 0.0,
            "realized_pnl_af_model_usd": 0.0,
            "overlay_equity_usd": state.get(
                "overlay_equity_usd", self.overlay_start_usd
            ),
            "skipped_for_size": 0,
            "orders_blocked_kill_switch": 0,
            "size_capped": 0,
            "exit_failures": 0,
            "missed_day": False,
            "catchup_bars": 0,
            "live_since": live_since.isoformat() if live_since else None,
            "no_trade_reason": None,
        }
        if state.get("halted"):
            self._phase = PHASE_HALTED
            # F8 follow-through: a quarantined start reconciles
            # the account once (flat -> stay halted; holding ->
            # orphan policy) before the plain halted return.
            if state.get("state_quarantined") and not state.get(
                "quarantine_reconciled"
            ):
                self._reconcile_quarantine(day, state)
            self.log.emit(
                "runner_start_halted",
                ts=self._clock(),
                date=day.isoformat(),
                halt_reason=state.get("halt_reason"),
            )
            summary["halted"] = True
            summary["halt_reason"] = state.get("halt_reason")
            return summary

        # F6: resolve the market schedule BEFORE the day starts —
        # the flatten time it yields may be earlier than 15:55.
        schedule = self._resolve_schedule(day)
        self.log.emit(
            "runner_day_start",
            ts=self._clock(),
            date=day.isoformat(),
            enabled=self.enabled,
            live_since=live_since.isoformat() if live_since else None,
            feed_note=FEED_NOTE,
            calendar_status=schedule["status"],
            market_close=(
                schedule["market_close"].strftime("%H:%M")
                if schedule["market_close"] else None
            ),
            early_close=bool(
                schedule["market_close"]
                and schedule["market_close"] < time(16, 0)
            ),
            flatten_time=self._flatten_time.strftime("%H:%M"),
        )
        try:
            self._reconcile(day, state)
        except ReconciliationHalt as exc:
            return self._halt(day, state, summary, str(exc))
        # Persist any overnight booking + the restored position.
        state["overlay_equity_usd"] = summary["overlay_equity_usd"] = round(
            self.overlay_start_usd
            + float(state.get("realized_pnl_total_usd") or 0.0),
            2,
        )
        if schedule["non_trading"]:
            # F6: the calendar says today is not a trading day —
            # a clean skip (reconciliation above already handled
            # any leftover/orphan position).
            self._phase = PHASE_DONE
            summary["no_trade_reason"] = "non_trading_day"
            self.log.emit(
                "non_trading_day",
                ts=self._clock(),
                date=day.isoformat(),
                source="alpaca_calendar",
            )
            self._save(state)
            summary["overlay_equity_usd"] = state["overlay_equity_usd"]
            self.log.emit("day_summary", ts=self._clock(), **summary)
            return summary
        # Day context for the F11 wall-clock backstop: the live
        # state/summary dicts this run mutates.
        self._day_state = state
        self._day_summary = summary
        # The session's live frontier is part of the day's persisted
        # state: catch-up mode is explicit, never implicit.
        self._live_since = live_since
        state["live_since"] = live_since.isoformat() if live_since else None
        state["catchup_bars"] = 0
        state["missed_day"] = None
        resumed = self._pos is not None
        self._phase = PHASE_IN_POSITION if resumed else PHASE_IDLE
        self._save(state)

        strat = STRATEGY_FACTORIES["orb15"](SPY_META)
        strat.start_day(day.isoformat())
        entry_seen = False       # the strategy fired its entry signal
        entry_adopted = False    # ...and it matched a restored position
        strat_trades_seen = 0
        done_logged = False
        # Live-frontier bookkeeping (see the module docstring): a
        # catch-up entry is remembered here until its replayed
        # outcome is known, then logged once as a missed_day.
        missed: Optional[dict] = None
        missed_logged = False
        missed_record: Optional[dict] = None
        frontier_reached = live_since is None
        catchup_bars = 0
        last_catchup_close: Optional[float] = None

        def book_strategy_exit(trade, fill, bar, managed_by=None) -> None:
            """Book a strategy-decided exit fill. A PARTIAL fill
            (F4) leaves the remainder under runner management at
            the recorded stop/target for the rest of the day —
            it is never abandoned to the end-of-data flatten."""
            pnl = self._book_exit(fill)
            self._apply_pnl(state, summary, pnl, fill.qty)
            summary["trades"] += 1
            fields = {
                "direction": trade.direction,
                "exit_reason": trade.exit_reason,
                "strategy_exit_price": trade.exit_price,
                "fill_exit_price": fill.price,
                "exit_order_id": fill.order_id,
                "pnl_usd": pnl,
                "overlay_equity_usd": summary["overlay_equity_usd"],
            }
            if managed_by is not None:
                fields["managed_by"] = managed_by
            self.log.emit(
                "trade_closed", ts=bar.ts, date=day.isoformat(), **fields
            )
            if self._pos is None:
                self._remainder_managed = False
                self._remainder_bar_ts = None
                self._phase = PHASE_DONE
            else:
                self._remainder_managed = True
                # The remainder is managed from the NEXT bar on:
                # this bar's levels already produced this exit.
                self._remainder_bar_ts = bar.ts
                self._phase = PHASE_IN_POSITION
                self.log.emit(
                    "exit_partial_remainder",
                    ts=bar.ts,
                    date=day.isoformat(),
                    remaining_qty=self._pos.qty,
                    stop=self._pos.stop,
                    target=self._pos.target,
                    note=(
                        "partial exit fill: the remainder stays "
                        "under stop/target management for the rest "
                        "of the day (hardening F4)"
                    ),
                )
            self._save(state)

        def handle_exit_from_strategy(trade, bar) -> None:
            """The strategy closed its simulated trade on this bar:
            mirror it with a real market order (if we hold one).
            The order goes through the F3 exit chain: a failure
            never crashes the run — the exit becomes PENDING and
            is retried on subsequent bars."""
            nonlocal strat_trades_seen, missed_logged, missed_record
            strat_trades_seen += 1
            if self._pos is None:
                self.log.emit(
                    "strategy_trade_closed_observe_only",
                    ts=bar.ts,
                    date=day.isoformat(),
                    exit_reason=trade.exit_reason,
                    strategy_exit_price=trade.exit_price,
                    strategy_pnl_usd=trade.pnl_usd,
                )
                if missed is not None and not missed_logged:
                    # The catch-up trade resolved inside the replay:
                    # the missed day now has its replayed outcome.
                    missed_logged = True
                    missed_record = self._log_missed_day(
                        day, missed, outcome="closed_in_replay", trade=trade
                    )
                    state["missed_day"] = missed_record
                    self._save(state)
                return
            fill = self._exit_fill(
                self._pos.qty,
                _exit_side(self._pos.direction),
                context="strategy_exit",
                ts=bar.ts,
            )
            if fill is None:
                # F3: both the market order and close_position
                # failed (already logged critical). Keep managing:
                # the exit is retried at the top of every bar.
                self._exit_pending_reason = trade.exit_reason
                self._exit_pending_trade = trade
                self.log.emit(
                    "exit_pending_retry",
                    ts=bar.ts,
                    date=day.isoformat(),
                    exit_reason=trade.exit_reason,
                    qty=self._pos.qty,
                )
                return
            book_strategy_exit(trade, fill, bar)

        for bar in bars:
            if not _is_rth(bar):
                continue
            # --- live-frontier classification (before the strategy
            # consumes the bar): a bar already complete when this
            # run began consuming the day is catch-up history.
            if not self._bar_is_live(bar):
                catchup_bars += 1
                last_catchup_close = bar.close
            elif not frontier_reached:
                # First live bar: catch-up is over. A replayed trade
                # still open at the frontier makes this a missed
                # day NOW — the runner does not enter late.
                frontier_reached = True
                summary["catchup_bars"] = catchup_bars
                state["catchup_bars"] = catchup_bars
                self._save(state)
                self.log.emit(
                    "live_frontier_reached",
                    ts=bar.ts,
                    date=day.isoformat(),
                    catchup_bars=catchup_bars,
                    live_since=(
                        live_since.isoformat() if live_since else None
                    ),
                )
                if (
                    missed is not None
                    and not missed_logged
                    and self._pos is None
                    and strat.position is not None
                ):
                    missed_logged = True
                    missed_record = self._log_missed_day(
                        day,
                        missed,
                        outcome="open_at_frontier",
                        mark_price=(
                            last_catchup_close
                            if last_catchup_close is not None
                            else bar.close
                        ),
                    )
                    state["missed_day"] = missed_record
                    self._save(state)
            # F11 bookkeeping: the wall-clock moment this bar was
            # processed (the backstop's staleness yardstick).
            self._last_bar_at = self._clock()
            # --- F3 exit retry: a strategy exit whose orders ALL
            # failed on an earlier bar is retried before anything
            # else consumes this one.
            if self._exit_pending_reason is not None and self._pos is not None:
                retry_fill = self._exit_fill(
                    self._pos.qty,
                    _exit_side(self._pos.direction),
                    context="exit_retry",
                    ts=bar.ts,
                )
                if retry_fill is not None:
                    pending_trade = self._exit_pending_trade
                    self._exit_pending_reason = None
                    self._exit_pending_trade = None
                    book_strategy_exit(
                        pending_trade, retry_fill, bar,
                        managed_by="exit_retry",
                    )
            strat.on_bar(bar)

            # --- strategy exit (its simulated trade closed this bar)
            if len(strat.trades) > strat_trades_seen:
                handle_exit_from_strategy(strat.trades[-1], bar)

            # --- entry transition
            if strat.position is not None and not entry_seen:
                entry_seen = True
                sp = strat.position
                if self._pos is not None:
                    # Restart adoption: the strategy re-fired the
                    # recorded entry. It must agree exactly — where
                    # "exactly" means the CAPPED order quantity the
                    # entry actually placed (F5), since a binding
                    # cap legitimately makes them differ.
                    expected_qty = self._capped_qty(
                        sp.units, sp.entry_price
                    )[0]
                    if (
                        sp.direction != self._pos.direction
                        or expected_qty != self._pos.qty
                    ):
                        return self._halt(
                            day, state, summary,
                            "restart rebuild mismatch: strategy "
                            f"re-fired {sp.direction} {sp.units} sh "
                            f"but state holds {self._pos.direction} "
                            f"{self._pos.qty} sh",
                        )
                    entry_adopted = True
                    self.log.emit(
                        "entry_adopted_after_restart",
                        ts=bar.ts,
                        date=day.isoformat(),
                        position=self._pos.to_state(),
                    )
                else:
                    # A fresh signal (no restored position).
                    if missed is not None or not self._bar_is_live(bar):
                        # LIVE-FRONTIER GATE: the signal bar is
                        # history (start-of-day catch-up), or the
                        # day was already missed by an earlier
                        # catch-up signal. The decision is logged
                        # as a catch-up event and NO order is
                        # placed — the runner never trades the past
                        # and never enters late.
                        if missed is None:
                            missed = {
                                "direction": sp.direction,
                                "entry_time": sp.entry_time,
                                "strategy_entry_price": sp.entry_price,
                                "stop": sp.stop,
                                "target": sp.target,
                                "shares": sp.units,
                                "signal_bar": bar.ts.isoformat(),
                                "live_since": (
                                    live_since.isoformat()
                                    if live_since
                                    else None
                                ),
                            }
                            summary["missed_day"] = True
                            summary["no_trade_reason"] = "missed_day"
                            state["missed_day"] = missed
                            self.log.emit(
                                "catchup_signal",
                                ts=bar.ts,
                                date=day.isoformat(),
                                **missed,
                            )
                        else:
                            self.log.emit(
                                "signal_ignored_after_missed_day",
                                ts=bar.ts,
                                date=day.isoformat(),
                                direction=sp.direction,
                                shares=sp.units,
                            )
                        self._phase = PHASE_DONE
                        self._save(state)
                    else:
                        # A fresh LIVE signal: log it, then block it
                        # (kill switch) or fill it — through the
                        # gated entry placement, never directly.
                        self.log.emit(
                            "signal",
                            ts=bar.ts,
                            date=day.isoformat(),
                            direction=sp.direction,
                            entry_time=sp.entry_time,
                            strategy_entry_price=sp.entry_price,
                            stop=sp.stop,
                            target=sp.target,
                            shares=sp.units,
                        )
                        if not self.enabled:
                            summary["orders_blocked_kill_switch"] += 1
                            self.log.emit(
                                "order_blocked_kill_switch",
                                ts=bar.ts,
                                date=day.isoformat(),
                                direction=sp.direction,
                                shares=sp.units,
                            )
                            self._phase = PHASE_DONE
                        else:
                            # F5: hard caps bind BEFORE the order —
                            # the trade is booked at its actual
                            # (capped) size/risk, and the clipping
                            # is on the record.
                            order_qty, cap_info = self._capped_qty(
                                sp.units, sp.entry_price
                            )
                            if cap_info is not None:
                                summary["size_capped"] += 1
                                self.log.emit(
                                    "size_capped",
                                    ts=bar.ts,
                                    date=day.isoformat(),
                                    **cap_info,
                                )
                            if order_qty <= 0:
                                self.log.emit(
                                    "order_blocked_size_cap",
                                    ts=bar.ts,
                                    date=day.isoformat(),
                                    direction=sp.direction,
                                    spec_shares=sp.units,
                                )
                                self._phase = PHASE_DONE
                                self._save(state)
                                continue
                            fill = self._place_entry_order(
                                bar, order_qty, _entry_side(sp.direction)
                            )
                            self._pos = RunnerPosition(
                                direction=sp.direction,
                                qty=fill.qty,
                                entry_price=fill.price,
                                strategy_entry_price=sp.entry_price,
                                entry_time=sp.entry_time,
                                stop=sp.stop,
                                target=sp.target,
                                entry_order_id=fill.order_id,
                            )
                            if fill.qty != order_qty:
                                self.log.emit(
                                    "entry_partial_fill",
                                    ts=bar.ts,
                                    date=day.isoformat(),
                                    wanted=order_qty,
                                    filled=fill.qty,
                                )
                            self.log.emit(
                                "order_filled",
                                ts=bar.ts,
                                date=day.isoformat(),
                                kind="entry",
                                order_id=fill.order_id,
                                client_order_id=self._client_order_id(
                                    "entry"
                                ),
                                fill_price=fill.price,
                                qty=fill.qty,
                            )
                            self._phase = PHASE_IN_POSITION
                            self._save(state)

            # --- orphan management: we hold a KNOWN position the
            # strategy doesn't (restart with a feed gap). Manage it
            # from the recorded levels with the spec's semantics;
            # the 15:55 flatten is enforced here even then. This
            # engages only once no (re-)entry can fire — the entry
            # window has closed or the strategy is done — because
            # during a same-day rebuild the pre-entry bars are
            # history the position must not be managed against.
            if (
                self._pos is not None
                and strat.position is None
                and len(strat.trades) == strat_trades_seen
                and resumed
                and not entry_adopted
                and (strat.done or bar.ts.time() >= ENTRY_CUTOFF)
            ):
                reason = self._orphan_exit_reason(bar)
                if reason is not None:
                    pos = self._pos
                    fill = self._exit_fill(
                        pos.qty, _exit_side(pos.direction),
                        context="orphan_levels", ts=bar.ts,
                    )
                    if fill is not None:
                        pnl = self._book_exit(fill)
                        self._apply_pnl(state, summary, pnl, fill.qty)
                        summary["trades"] += 1
                        self.log.emit(
                            "trade_closed",
                            ts=bar.ts,
                            date=day.isoformat(),
                            direction=pos.direction,
                            exit_reason=reason,
                            managed_by="runner_orphan_levels",
                            fill_exit_price=fill.price,
                            exit_order_id=fill.order_id,
                            pnl_usd=pnl,
                            overlay_equity_usd=summary["overlay_equity_usd"],
                        )
                        if self._pos is None:
                            self._phase = PHASE_DONE
                        self._save(state)
                    # fill is None: exit_failure_critical already
                    # logged; the position stays under management
                    # and the level check runs again on later bars.

            # --- remainder management (F4): a partial strategy
            # exit left shares behind. They stay under the
            # recorded stop/target/flatten levels for the rest of
            # the day instead of riding unmanaged to end-of-data.
            if (
                self._pos is not None
                and self._remainder_managed
                and self._remainder_bar_ts is not None
                and bar.ts > self._remainder_bar_ts
                and self._exit_pending_reason is None
                and strat.position is None
            ):
                reason = self._orphan_exit_reason(bar)
                if reason is not None:
                    pos = self._pos
                    fill = self._exit_fill(
                        pos.qty, _exit_side(pos.direction),
                        context="exit_remainder", ts=bar.ts,
                    )
                    if fill is not None:
                        pnl = self._book_exit(fill)
                        self._apply_pnl(state, summary, pnl, fill.qty)
                        summary["trades"] += 1
                        self.log.emit(
                            "trade_closed",
                            ts=bar.ts,
                            date=day.isoformat(),
                            direction=pos.direction,
                            exit_reason=reason,
                            managed_by="runner_remainder_levels",
                            fill_exit_price=fill.price,
                            exit_order_id=fill.order_id,
                            pnl_usd=pnl,
                            overlay_equity_usd=summary["overlay_equity_usd"],
                        )
                        if self._pos is None:
                            self._remainder_managed = False
                            self._remainder_bar_ts = None
                            self._phase = PHASE_DONE
                        self._save(state)

            # --- skipped / void detection (once)
            if (
                not done_logged
                and strat.done
                and self._pos is None
                and not strat.trades
                and not entry_seen
                and summary["trades"] == 0
            ):
                done_logged = True
                if strat.skipped_for_size > 0:
                    summary["skipped_for_size"] += strat.skipped_for_size
                    summary["no_trade_reason"] = "skipped_for_size"
                    self.log.emit(
                        "signal_skipped_size",
                        ts=bar.ts, date=day.isoformat(),
                        skipped_for_size=strat.skipped_for_size,
                    )
                elif getattr(strat, "_range_high", None) is None:
                    summary["no_trade_reason"] = "no_opening_range_bars"
                    self.log.emit(
                        "no_range_day", ts=bar.ts, date=day.isoformat()
                    )
                else:
                    summary["no_trade_reason"] = "signal_void_degenerate"
                    self.log.emit(
                        "signal_void", ts=bar.ts, date=day.isoformat()
                    )
                self._phase = PHASE_DONE

            # --- phase bookkeeping
            if self._pos is not None:
                self._phase = PHASE_IN_POSITION
            elif self._phase != PHASE_DONE:
                if strat.done:
                    self._phase = PHASE_DONE
                elif getattr(strat, "_range_done", False):
                    self._phase = PHASE_ARMED
                else:
                    self._phase = PHASE_RANGE_BUILDING

        # --- a catch-up entry whose replayed trade never resolved
        # inside the data (frontier never crossed, trade still
        # open): record the missed day here, marked at the last
        # catch-up close.
        if missed is not None and not missed_logged:
            missed_logged = True
            missed_record = self._log_missed_day(
                day,
                missed,
                outcome="open_at_end_of_data",
                mark_price=last_catchup_close,
            )

        # --- end of bars: a known position still open is flattened
        # NOW (feed ended / day over). Never carried overnight.
        # The exit goes through the F3 chain: if even that fails,
        # the position stays in the persisted state (tomorrow's
        # reconciliation flattens it as an overnight leftover) and
        # the failure is on the record — the run does not crash.
        if self._pos is not None:
            pos = self._pos
            fill = self._exit_fill(
                pos.qty, _exit_side(pos.direction),
                context="end_of_data_flatten",
            )
            if fill is None:
                self.log.emit(
                    "flatten_failed_position_carried",
                    ts=self._clock(),
                    date=day.isoformat(),
                    direction=pos.direction,
                    qty=pos.qty,
                    note=(
                        "end-of-data flatten failed via market order "
                        "AND close_position; the position remains in "
                        "the persisted state for tomorrow's "
                        "overnight-leftover flatten"
                    ),
                )
            else:
                pnl = self._book_exit(fill)
                self._apply_pnl(state, summary, pnl, fill.qty)
                summary["trades"] += 1
                self.log.emit(
                    "flatten_enforced",
                    ts=self._clock(),
                    date=day.isoformat(),
                    direction=pos.direction,
                    fill_exit_price=fill.price,
                    exit_order_id=fill.order_id,
                    pnl_usd=pnl,
                    overlay_equity_usd=summary["overlay_equity_usd"],
                )
                if self._pos is None:
                    self._phase = PHASE_DONE
                else:
                    self.log.emit(
                        "flatten_partial_position_carried",
                        ts=self._clock(),
                        date=day.isoformat(),
                        remaining_qty=self._pos.qty,
                        note=(
                            "the flatten filled partially; the "
                            "remainder remains in the persisted "
                            "state for tomorrow's reconciliation"
                        ),
                    )

        if summary["no_trade_reason"] is None and summary["trades"] == 0:
            if strat.skipped_for_size > 0:
                summary["skipped_for_size"] = strat.skipped_for_size
                summary["no_trade_reason"] = "skipped_for_size"
            elif entry_seen or resumed:
                summary["no_trade_reason"] = None
            elif getattr(strat, "_range_high", None) is None:
                summary["no_trade_reason"] = "no_opening_range_bars"
            else:
                summary["no_trade_reason"] = "no_breakout"
        if self._phase != PHASE_HALTED:
            self._phase = PHASE_DONE
        state["catchup_bars"] = catchup_bars
        summary["catchup_bars"] = catchup_bars
        summary["exit_failures"] = self._exit_failures
        if missed_record is not None:
            state["missed_day"] = missed_record
        self._save(state)
        summary["overlay_equity_usd"] = state["overlay_equity_usd"]
        self.log.emit("day_summary", ts=self._clock(), **summary)
        # The run is over: the F11 backstop stands down until the
        # next run_day installs a fresh day context.
        self._day_state = None
        self._day_summary = None
        return summary

    def _orphan_exit_reason(self, bar: Bar) -> Optional[str]:
        """Spec management for a position the strategy can't see:
        flatten bar first, then stop before target (stop wins ties),
        exits decided on the completed bar, filled at market."""
        pos = self._pos
        assert pos is not None
        if bar.ts.time() >= self._flatten_time:
            return "EOD_FLATTEN"
        if pos.direction == "long":
            if bar.low <= pos.stop:
                return "STOP"
            if bar.high >= pos.target:
                return "TARGET"
        else:
            if bar.high >= pos.stop:
                return "STOP"
            if bar.low <= pos.target:
                return "TARGET"
        return None

    def _log_missed_day(
        self,
        day: date_type,
        missed: dict,
        *,
        outcome: str,
        trade=None,
        mark_price: Optional[float] = None,
    ) -> dict:
        """Record a missed day: a catch-up (historical) entry signal
        the gated runner correctly refused to trade. The replayed
        outcome is logged FOR THE RECORD ONLY — nothing is booked
        to the overlay. Returns the record (also persisted in the
        day state). ``outcome`` is ``closed_in_replay`` (trade =
        the strategy's simulated Trade), ``open_at_frontier``, or
        ``open_at_end_of_data`` (marked at mark_price)."""
        fields = {"date": day.isoformat(), **missed, "outcome": outcome}
        if trade is not None:
            fields.update(
                exit_reason=trade.exit_reason,
                strategy_exit_price=trade.exit_price,
                strategy_pnl_usd=trade.pnl_usd,
                strategy_pnl_af_model_usd=round(
                    trade.pnl_usd
                    - self.friction_per_share_usd * missed["shares"],
                    2,
                ),
            )
        if mark_price is not None:
            sign = 1.0 if missed["direction"] == "long" else -1.0
            fields["mark_price"] = mark_price
            fields["strategy_unrealized_usd"] = round(
                sign
                * (mark_price - missed["strategy_entry_price"])
                * missed["shares"],
                2,
            )
        fields["booked"] = False
        self.log.emit("missed_day", ts=self._clock(), **fields)
        return fields

    def _apply_pnl(self, state: dict, summary: dict, pnl: float, qty: int) -> None:
        state["realized_pnl_total_usd"] = round(
            float(state.get("realized_pnl_total_usd") or 0.0) + pnl, 2
        )
        state["overlay_equity_usd"] = round(
            self.overlay_start_usd + state["realized_pnl_total_usd"], 2
        )
        summary["realized_pnl_usd"] = round(
            summary["realized_pnl_usd"] + pnl, 2
        )
        summary["realized_pnl_af_model_usd"] = round(
            summary["realized_pnl_af_model_usd"]
            + pnl
            - self.friction_per_share_usd * qty,
            2,
        )
        summary["overlay_equity_usd"] = state["overlay_equity_usd"]

    def _halt(self, day: date_type, state: dict, summary: dict, reason: str) -> dict:
        self._phase = PHASE_HALTED
        state["halted"] = True
        state["halt_reason"] = reason
        self._save(state)
        self.log.emit(
            "reconciliation_halt", ts=self._clock(),
            date=day.isoformat(), reason=reason,
        )
        summary["halted"] = True
        summary["halt_reason"] = reason
        summary["exit_failures"] = self._exit_failures
        self._day_state = None
        self._day_summary = None
        self.log.emit("day_summary", ts=self._clock(), **summary)
        return summary

    # -------------------------------------------------------------- service
    def serve(
        self,
        source_factory: Callable[[date_type], Iterable[Bar]],
        sleep_fn: Callable[[float], None] = time_module.sleep,
        max_days: Optional[int] = None,
        watchdog_poll_seconds: float = 20.0,
    ) -> Optional[dict]:
        """Run forever (or ``max_days`` days): hours-gated so the
        service may run 24/7 harmlessly. Only ET weekdays inside
        09:20–16:10 trigger a day run; a halted day stops the loop.

        A daemon watchdog thread runs alongside (hardening F11):
        while a day run is blocked inside a stalled bar source —
        the case where bars simply stop arriving — the watchdog
        checks the wall clock and fires the flatten backstop when
        a position is open past its flatten time with no bar
        processed for over 3 minutes. It can never kill the
        service: its own exceptions are swallowed (the backstop
        logs its failures itself)."""
        stop_watchdog = threading.Event()

        def _watchdog() -> None:
            while not stop_watchdog.wait(watchdog_poll_seconds):
                try:
                    self._maybe_backstop_flatten(self._clock())
                except Exception:
                    pass

        watchdog = threading.Thread(
            target=_watchdog, name="orb15-flatten-watchdog", daemon=True
        )
        watchdog.start()
        last_run: Optional[date_type] = None
        days_run = 0
        try:
            while True:
                now = self._clock()
                today = now.date()
                in_window = (
                    now.weekday() < 5
                    and time(9, 20) <= now.time() < time(16, 10)
                    and last_run != today
                )
                if in_window:
                    summary = self.run_day(
                        today, source_factory(today), live_since=now
                    )
                    last_run = today
                    days_run += 1
                    if summary.get("halted"):
                        return summary
                    if max_days is not None and days_run >= max_days:
                        return summary
                else:
                    sleep_fn(15.0)
                    if max_days is not None and days_run >= max_days:
                        return None
        finally:
            stop_watchdog.set()


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alpaca.runner",
        description="ORB-15 forward test on the Alpaca PAPER account.",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="state + event-log directory "
        "(default: $ALPACA_RUNNER_STATE_DIR or ./alpaca_state)",
    )
    args = parser.parse_args(argv)

    state_dir = Path(
        args.state_dir
        or os.environ.get(ENV_STATE_DIR)
        or "alpaca_state"
    )
    # F7: single instance. The lock is taken BEFORE any API use;
    # a second instance logs the refusal and exits CLEANLY (0), so
    # systemd never crash-loops a duplicate and it can never
    # double-trade the account.
    lock = InstanceLock(state_dir / "runner.lock")
    if not lock.acquire():
        print(
            "REFUSING TO START: another instance of the ORB-15 "
            "runner already holds the lock in "
            f"{state_dir} (single-instance guard). Exiting cleanly.",
            file=sys.stderr,
        )
        try:
            EventLog(state_dir / "events.jsonl").emit(
                "runner_lock_refused", pid=os.getpid(),
                note="a second instance refused to start (F7)",
            )
        except Exception:
            pass
        return 0
    try:
        return _main_locked(args, state_dir)
    finally:
        lock.release()


def _parse_hhmm(raw: str) -> time:
    parts = str(raw).split(":")
    return time(int(parts[0]), int(parts[1]))


def _calendar_fn_for(client: AlpacaClient):
    """The F6 calendar provider backed by the real client: the
    day's {"open", "close"} times, or None for a non-trading day."""

    def calendar_fn(day: date_type):
        for entry in client.get_calendar(day, day):
            if str(entry.get("date")) == day.isoformat():
                return {
                    "open": _parse_hhmm(entry.get("open", "09:30")),
                    "close": _parse_hhmm(entry.get("close", "16:00")),
                }
        return None

    return calendar_fn


def _main_locked(args, state_dir: Path) -> int:
    try:
        config = AlpacaConfig.from_env()
    except MissingCredentialsError as exc:
        # Clean startup refusal — not a crash loop.
        print(f"REFUSING TO START: {exc}", file=sys.stderr)
        return 2
    client = AlpacaClient(config)
    try:
        account = client.verify_paper()
    except (AlpacaAPIError, PaperOnlyError) as exc:
        print(f"REFUSING TO START: paper account check failed: {exc}",
              file=sys.stderr)
        return 2

    enabled = runner_enabled()
    broker = AlpacaBroker(client)
    log = EventLog(state_dir / "events.jsonl")
    store = StateStore(state_dir / "runner_state.json")
    runner = Orb15Runner(
        broker, enabled=enabled, log=log, store=store,
        calendar_fn=_calendar_fn_for(client),
    )
    source = AlpacaBarSource(client, clock=lambda: datetime.now(ET))

    print(f"ORB-15 Alpaca PAPER forward test — account "
          f"{account.get('account_number', '?')} status "
          f"{account.get('status', '?')}")
    print(f"orders enabled (ALPACA_RUNNER_ENABLED): {enabled}")
    print(f"FEED NOTE: {FEED_NOTE}")
    log.emit("runner_process_start", enabled=enabled,
             account_status=account.get("status"))
    summary = runner.serve(source.bars_for_day)
    if summary and summary.get("halted"):
        print(f"HALTED: {summary.get('halt_reason')}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
