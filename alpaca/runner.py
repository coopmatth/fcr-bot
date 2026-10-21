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
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time as time_module
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
    """What the runner needs from a broker: signed position and
    market-order fills. AlpacaBroker is production; SimulatedBroker
    is replay/test machinery."""

    def position_qty(self, symbol: str) -> int:
        ...

    def place_market(self, symbol: str, qty: int, side: str) -> Fill:
        ...


class AlpacaBroker:
    """Broker over AlpacaClient (PAPER). Places a market order and
    polls it to a fill; a partially-filled order that reaches a final
    state fills for the filled quantity only — never assumed."""

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

    def position_qty(self, symbol: str) -> int:
        pos = self._client.get_position(symbol)
        if pos is None:
            return 0
        return int(float(pos.get("qty") or 0))

    def place_market(self, symbol: str, qty: int, side: str) -> Fill:
        order = self._client.place_market_order(symbol, qty, side)
        order_id = order["id"]
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
            self._sleep(self._poll_seconds)
        raise OrderError(
            f"Alpaca order {order_id} did not fill after "
            f"{self._max_polls} polls; refusing to assume a fill."
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
    ):
        self.broker = broker
        self.enabled = enabled
        self.log = log or EventLog()
        self.store = store or StateStore()
        self._clock = clock or (lambda: datetime.now(ET))
        self.symbol = symbol
        self.overlay_start_usd = float(overlay_start_usd)
        self.friction_per_share_usd = float(friction_per_share_usd)
        self._pos: Optional[RunnerPosition] = None
        self._phase = PHASE_IDLE
        # The current day run's live frontier (see run_day). None =
        # no frontier: every bar counts as live (offline replay).
        self._live_since: Optional[datetime] = None

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
            fill = self.broker.place_market(
                self.symbol, pos.qty, _exit_side(pos.direction)
            )
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
            raise ReconciliationHalt(
                f"paper account holds {broker_qty} {self.symbol} but "
                "the runner state has no position — unknown holding, "
                "refusing to guess"
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
        state = self.store.load() or self._fresh_state(day)
        if state.get("date") != day.isoformat() and not state.get("position"):
            # New day, flat: roll the date, keep the overlay equity.
            state["date"] = day.isoformat()
        self._pos = None
        self._phase = PHASE_IDLE
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
            "missed_day": False,
            "catchup_bars": 0,
            "live_since": live_since.isoformat() if live_since else None,
            "no_trade_reason": None,
        }
        if state.get("halted"):
            self._phase = PHASE_HALTED
            self.log.emit(
                "runner_start_halted",
                ts=self._clock(),
                date=day.isoformat(),
                halt_reason=state.get("halt_reason"),
            )
            summary["halted"] = True
            summary["halt_reason"] = state.get("halt_reason")
            return summary

        self.log.emit(
            "runner_day_start",
            ts=self._clock(),
            date=day.isoformat(),
            enabled=self.enabled,
            live_since=live_since.isoformat() if live_since else None,
            feed_note=FEED_NOTE,
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

        def handle_exit_from_strategy(trade, bar) -> None:
            """The strategy closed its simulated trade on this bar:
            mirror it with a real market order (if we hold one)."""
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
            fill = self.broker.place_market(
                self.symbol, self._pos.qty, _exit_side(self._pos.direction)
            )
            pnl = self._book_exit(fill)
            self._apply_pnl(state, summary, pnl, fill.qty)
            summary["trades"] += 1
            self.log.emit(
                "trade_closed",
                ts=bar.ts,
                date=day.isoformat(),
                direction=trade.direction,
                exit_reason=trade.exit_reason,
                strategy_exit_price=trade.exit_price,
                fill_exit_price=fill.price,
                exit_order_id=fill.order_id,
                pnl_usd=pnl,
                overlay_equity_usd=summary["overlay_equity_usd"],
            )
            self._phase = PHASE_DONE
            self._save(state)

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
                    # recorded entry. It must agree exactly.
                    if (
                        sp.direction != self._pos.direction
                        or sp.units != self._pos.qty
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
                            fill = self._place_entry_order(
                                bar, sp.units, _entry_side(sp.direction)
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
                            if fill.qty != sp.units:
                                self.log.emit(
                                    "entry_partial_fill",
                                    ts=bar.ts,
                                    date=day.isoformat(),
                                    wanted=sp.units,
                                    filled=fill.qty,
                                )
                            self.log.emit(
                                "order_filled",
                                ts=bar.ts,
                                date=day.isoformat(),
                                kind="entry",
                                order_id=fill.order_id,
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
                    fill = self.broker.place_market(
                        self.symbol, pos.qty, _exit_side(pos.direction)
                    )
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
        if self._pos is not None:
            pos = self._pos
            fill = self.broker.place_market(
                self.symbol, pos.qty, _exit_side(pos.direction)
            )
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
            self._phase = PHASE_DONE

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
        if missed_record is not None:
            state["missed_day"] = missed_record
        self._save(state)
        summary["overlay_equity_usd"] = state["overlay_equity_usd"]
        self.log.emit("day_summary", ts=self._clock(), **summary)
        return summary

    def _orphan_exit_reason(self, bar: Bar) -> Optional[str]:
        """Spec management for a position the strategy can't see:
        flatten bar first, then stop before target (stop wins ties),
        exits decided on the completed bar, filled at market."""
        pos = self._pos
        assert pos is not None
        if bar.ts.time() >= FLATTEN_TIME:
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
        self.log.emit("day_summary", ts=self._clock(), **summary)
        return summary

    # -------------------------------------------------------------- service
    def serve(
        self,
        source_factory: Callable[[date_type], Iterable[Bar]],
        sleep_fn: Callable[[float], None] = time_module.sleep,
        max_days: Optional[int] = None,
    ) -> Optional[dict]:
        """Run forever (or ``max_days`` days): hours-gated so the
        service may run 24/7 harmlessly. Only ET weekdays inside
        09:20–16:10 trigger a day run; a halted day stops the loop."""
        last_run: Optional[date_type] = None
        days_run = 0
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

    state_dir = Path(
        args.state_dir
        or os.environ.get(ENV_STATE_DIR)
        or "alpaca_state"
    )
    enabled = runner_enabled()
    broker = AlpacaBroker(client)
    log = EventLog(state_dir / "events.jsonl")
    store = StateStore(state_dir / "runner_state.json")
    runner = Orb15Runner(broker, enabled=enabled, log=log, store=store)
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
