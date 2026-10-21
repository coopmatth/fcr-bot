"""ORB-15 OPTIONS variant runner on the Alpaca PAPER account.

PAPER ONLY. This runner trades the options expression frozen in
``ORB15_OPTIONS_SPEC.md``: the SIGNAL is the identical frozen
ORB-15 (``fcr.strategies_intraday.ORBStrategy`` — the very class
the validating backtest ran, fed the same SPY 1-minute bars);
the EXPRESSION is one bought SPY option contract per signal —
a CALL for a long breakout, a PUT for a short breakout — chosen
by the frozen selection rule in alpaca/options_select.py (nearest
expiry, ATM strike), gated on the snapshot ask (never above
$2.50/share, never stale, never blind), and exited when the
UNDERLYING reaches the strategy's stop/target levels or the
15:55 ET flatten bar. Long options only: the maximum loss on any
trade is the premium paid, by construction.

DESIGN — same shape as alpaca/runner.py, deliberately. This
module mirrors the shares runner's day loop (live-frontier gate
in the entry placement itself, catch-up/missed-day bookkeeping,
restart adoption, orphan management from recorded levels,
end-of-data flatten), its persistence (JSONL event log + atomic
JSON state, rewritten after every transition), its reconciliation
posture (any disagreement the state cannot fully explain stops
the day; an unexplained option position is FLATTENED under the
orphan policy and the day halts), and its safety rails (paper
host lock + PK-key refusal in alpaca.config/client, the
ALPACA_RUNNER_ENABLED kill switch blocking entries but never
exits, a single-instance flock). Where it differs, it differs
because options differ:

* STATE IS ITS OWN. State dir defaults to ``alpaca_options_state``
  (env ``ALPACA_OPTIONS_STATE_DIR``), with its own state file,
  event log and lock file. The two runners share the paper
  ACCOUNT but never state; each reconciles only its own
  instrument (shares runner: SPY shares; this runner: SPY OCC
  option contracts).
* SIZING is exactly 1 contract; the strategy's share count is
  logged on the signal for the record and otherwise ignored.
  (The strategy's own size gate still applies to the SIGNAL: a
  day it voids or skips for size has no signal, hence no trade.)
* ACCOUNTING is realized options P&L only: the $500 overlay moves
  by (exit premium − entry premium) × 100 × contracts from
  ACTUAL fills. There is no friction model — the options
  expression has no backtest to be comparable to.
* EXITS mirror the strategy's simulated close with a market
  sell; if that order fails, the runner falls back once to a
  whole-position close (DELETE /v2/positions/{contract}) and, if
  that also fails, logs ``exit_failure_critical`` and keeps
  retrying on later bars and at end of day (audit finding F3's
  contained fix, applied here from the start).

OPTIONS DATA NOTE — READ BEFORE TRUSTING THE PREMIUM GATE:
contract quotes come from Alpaca's FREE options feed
(``feed=indicative`` on /v1beta1/options/snapshots), which may be
DELAYED relative to real-time OPRA (a paid add-on). Market orders
execute against the real book regardless of our quote feed; the
gate is a cost control. It reads the snapshot's ask and the day
is skipped when the snapshot is missing, ask-less, or older than
15 minutes — never entered blind (spec rules 9–10). Booked
premiums are always the actual Alpaca fill prices.

STATUS: built 2026-10-21, NOT deployed. The deploy unit
(deploy/orb15-options-paper.service) is written but not
installed; the shares runner's forward test owns the box until
this variant is separately reviewed and installed.
"""
from __future__ import annotations

import argparse
import os
import sys
import time as time_module
from dataclasses import asdict, dataclass
from datetime import date as date_type
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional
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

from .client import AlpacaAPIError
from .config import (
    AlpacaConfig,
    MissingCredentialsError,
    PaperOnlyError,
    runner_enabled,
)
from .options_broker import (
    OptionsBroker,
    OptionsClient,
    QuoteError,
    StaleQuoteError,
)
from .options_select import is_option_symbol_for, select_contract
from .runner import (
    AlpacaBarSource,
    EventLog,
    Fill,
    InstanceLock,
    ReconciliationHalt,
    StateStore,
)

ET = ZoneInfo("America/New_York")

UNDERLYING = "SPY"
OVERLAY_START_USD = 500.0
CONTRACT_MULTIPLIER = 100  # shares per listed option contract
CONTRACTS_PER_TRADE = 1     # frozen: exactly one contract (spec rule 8)

# Premium gate (spec rules 9–10): skip the day when the snapshot
# ask is above this per-share cap ($250/contract — also the
# trade's max possible loss), or when the quote is older than
# this many minutes / unusable. One config place, like the
# shares runner's F5 caps.
PREMIUM_CAP_USD = 2.50
MAX_QUOTE_STALENESS_MINUTES = 15.0

ENV_OPTIONS_STATE_DIR = "ALPACA_OPTIONS_STATE_DIR"

PHASE_IDLE = "IDLE"
PHASE_RANGE_BUILDING = "RANGE_BUILDING"
PHASE_ARMED = "ARMED"
PHASE_IN_POSITION = "IN_POSITION"
PHASE_DONE = "DONE"
PHASE_HALTED = "HALTED"

OPTIONS_DATA_NOTE = (
    "Options quotes come from Alpaca's free indicative options "
    "feed (data.alpaca.markets /v1beta1/options/snapshots), which "
    "may be delayed vs real-time OPRA (paid). Market orders fill "
    "against the real book regardless; the premium gate reads the "
    "snapshot ask and skips the day when the snapshot is missing, "
    "ask-less, or older than 15 minutes."
)

OPTIONS_FEED_NOTE = (
    "Underlying signals come from Alpaca's free IEX real-time "
    "feed (~3% of consolidated volume), as in the shares runner; "
    "the validating backtest used Polygon SIP bars. The options "
    "expression has no backtest: this forward test is its first "
    "measurement."
)

# The frozen SPY meta, identical to alpaca.runner.SPY_META /
# run_strategies_intraday.SYMBOL_META. The strategy runs on it
# unchanged; only its share count is not used for sizing here.
SPY_META = SymbolMeta(
    symbol="SPY",
    point_value_usd=1.0,
    tick_size=0.01,
    max_units=None,
    friction_per_unit_usd=0.04,
    risk_budget_usd=150.0,
    instrument_type="equity",
)


def _is_rth(bar: Bar) -> bool:
    return SESSION_START <= bar.ts.time() < SESSION_END


@dataclass
class OptionsPosition:
    contract_symbol: str      # OCC symbol of the held contract
    option_type: str          # 'call' | 'put'
    direction: str            # the signal's direction: 'long' | 'short'
    expiry: str               # ISO date
    strike: float
    qty_contracts: int        # contracts held (positive; long only)
    entry_premium_usd: float  # ACTUAL fill, per share
    underlying_entry_price: float  # strategy entry (signal bar close)
    stop: float               # UNDERLYING stop level
    target: float             # UNDERLYING target level
    entry_time: str           # HH:MM ET of the signal bar
    entry_order_id: Optional[str] = None

    @property
    def entry_cost_usd(self) -> float:
        return round(
            self.entry_premium_usd * CONTRACT_MULTIPLIER * self.qty_contracts,
            2,
        )

    def to_state(self) -> dict:
        return asdict(self)

    @classmethod
    def from_state(cls, raw: dict) -> "OptionsPosition":
        return cls(**raw)


class Orb15OptionsRunner:
    """The ORB-15 options-variant forward-test engine (see the
    module docstring and ORB15_OPTIONS_SPEC.md)."""

    def __init__(
        self,
        broker,
        *,
        enabled: bool = False,
        log: Optional[EventLog] = None,
        store: Optional[StateStore] = None,
        clock: Optional[Callable[[], datetime]] = None,
        underlying: str = UNDERLYING,
        overlay_start_usd: float = OVERLAY_START_USD,
        premium_cap_usd: float = PREMIUM_CAP_USD,
        max_quote_staleness_minutes: float = MAX_QUOTE_STALENESS_MINUTES,
    ):
        self.broker = broker
        self.enabled = enabled
        self.log = log or EventLog()
        self.store = store or StateStore()
        self._clock = clock or (lambda: datetime.now(ET))
        self.underlying = underlying
        self.overlay_start_usd = float(overlay_start_usd)
        self.premium_cap_usd = float(premium_cap_usd)
        self.max_quote_staleness_minutes = float(max_quote_staleness_minutes)
        self._pos: Optional[OptionsPosition] = None
        self._phase = PHASE_IDLE
        self._live_since: Optional[datetime] = None
        self._exit_pending_reason: Optional[str] = None

    # ------------------------------------------------------------ state io
    def _fresh_state(self, day: date_type) -> dict:
        return {
            "version": 1,
            "variant": "orb15_options",
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

    # ------------------------------------------------------------- helpers
    def _client_order_id(self, day: date_type, purpose: str) -> str:
        """Deterministic per day + purpose (hardening F1 pattern):
        orb15opt-YYYY-MM-DD-entry / -exit-1 / -exit-2 ..."""
        return f"orb15opt-{day.isoformat()}-{purpose}"

    def _set_cid(self, day: date_type, purpose: str) -> None:
        setter = getattr(self.broker, "set_client_order_id", None)
        if setter is not None:
            setter(self._client_order_id(day, purpose))

    def _option_positions(self) -> dict:
        """The account's open SPY option positions: OCC symbol ->
        signed contracts. Non-option positions (e.g. the shares
        runner's SPY shares) are not this runner's business."""
        found = {}
        for raw in self.broker.list_positions():
            symbol = str(raw.get("symbol") or "")
            if not is_option_symbol_for(symbol, self.underlying):
                continue
            try:
                qty = int(float(raw.get("qty") or 0))
            except (TypeError, ValueError):
                continue
            if qty != 0:
                found[symbol] = qty
        return found

    def _book_exit(self, fill: Fill) -> float:
        """Book a closing fill against the open contract position:
        pnl from ACTUAL premiums. A partial fill leaves the
        remainder open at the same entry premium."""
        pos = self._pos
        assert pos is not None
        pnl = (fill.price - pos.entry_premium_usd) * CONTRACT_MULTIPLIER * fill.qty
        if fill.qty < pos.qty_contracts:
            self._pos = OptionsPosition(
                contract_symbol=pos.contract_symbol,
                option_type=pos.option_type,
                direction=pos.direction,
                expiry=pos.expiry,
                strike=pos.strike,
                qty_contracts=pos.qty_contracts - fill.qty,
                entry_premium_usd=pos.entry_premium_usd,
                underlying_entry_price=pos.underlying_entry_price,
                stop=pos.stop,
                target=pos.target,
                entry_time=pos.entry_time,
                entry_order_id=pos.entry_order_id,
            )
        else:
            self._pos = None
        return round(pnl, 2)

    def _apply_pnl(self, state: dict, summary: dict, pnl: float) -> None:
        state["realized_pnl_total_usd"] = round(
            float(state.get("realized_pnl_total_usd") or 0.0) + pnl, 2
        )
        state["overlay_equity_usd"] = round(
            self.overlay_start_usd + state["realized_pnl_total_usd"], 2
        )
        summary["realized_pnl_usd"] = round(summary["realized_pnl_usd"] + pnl, 2)
        summary["overlay_equity_usd"] = state["overlay_equity_usd"]

    # -------------------------------------------------------- reconciliation
    def _reconcile(self, day: date_type, state: dict) -> None:
        """Compare state vs the account's actual SPY option
        positions (spec rule 20). Raises ReconciliationHalt on any
        disagreement the state cannot fully explain — after first
        applying the ORPHAN POLICY to every unexplained position:
        flatten it, log it, then halt the day. A prior-day
        leftover that matches its state record exactly is
        flattened and BOOKED, and the day continues."""
        broker_positions = self._option_positions()
        raw_pos = state.get("position")
        state_pos = OptionsPosition.from_state(raw_pos) if raw_pos else None
        state_date = state.get("date")

        if state_date and state_date > day.isoformat():
            raise ReconciliationHalt(
                f"state file date {state_date} is after today "
                f"{day.isoformat()} — clock or state corruption"
            )

        matched = (
            state_pos is not None
            and broker_positions.get(state_pos.contract_symbol)
            == state_pos.qty_contracts
        )
        unexplained = {
            symbol: qty
            for symbol, qty in broker_positions.items()
            if not (
                state_pos is not None
                and symbol == state_pos.contract_symbol
                and qty == state_pos.qty_contracts
            )
        }

        if unexplained:
            # ORPHAN POLICY: flatten every unexplained contract
            # position, log each with full detail, then halt. An
            # unmanaged option position is never left riding.
            details = []
            for symbol, qty in sorted(unexplained.items()):
                fill = None
                flatten_error = None
                try:
                    fill = self.broker.close_position(symbol)
                except Exception as exc:  # noqa: BLE001 - logged, then halt
                    flatten_error = str(exc)
                self.log.emit(
                    "orphan_flattened",
                    ts=self._clock(),
                    date=day.isoformat(),
                    contract_symbol=symbol,
                    qty_contracts=qty,
                    fill_price=fill.price if fill else None,
                    proceeds_usd=(
                        round(fill.price * CONTRACT_MULTIPLIER * fill.qty, 2)
                        if fill
                        else None
                    ),
                    exit_order_id=fill.order_id if fill else None,
                    booked_pnl_usd=None,  # entry premium unknown: unbookable
                    flatten_error=flatten_error,
                    state_position_known=state_pos is not None,
                )
                details.append(f"{symbol} x{qty}")
                if (
                    state_pos is not None
                    and symbol == state_pos.contract_symbol
                    and fill is not None
                ):
                    # The state's own (mismatched) record described
                    # a position that is now closed: drop the record
                    # so the halt state is honest about being flat.
                    state["position"] = None
                    state_pos = None
            raise ReconciliationHalt(
                "unexplained SPY option position(s) on the paper "
                f"account: {', '.join(details)} — flattened per "
                "the orphan policy; halting the day for review"
            )

        if state_pos is not None and not matched:
            raise ReconciliationHalt(
                f"runner state claims {state_pos.qty_contracts} x "
                f"{state_pos.contract_symbol} but the paper account "
                "does not hold it — the position vanished outside "
                "the runner"
            )

        if state_pos is not None and state_date and state_date < day.isoformat():
            # Overnight leftover: a prior day's contract never
            # exited (the process must have died). It matches its
            # record exactly, so flattening + booking it is not a
            # guess.
            pos = state_pos
            self.log.emit(
                "overnight_leftover_detected",
                ts=self._clock(),
                date=day.isoformat(),
                position=pos.to_state(),
            )
            fill = self.broker.close_position(pos.contract_symbol)
            pnl = round(
                (fill.price - pos.entry_premium_usd)
                * CONTRACT_MULTIPLIER
                * fill.qty,
                2,
            )
            state["realized_pnl_total_usd"] = round(
                float(state.get("realized_pnl_total_usd") or 0.0) + pnl, 2
            )
            self.log.emit(
                "overnight_leftover_flattened",
                ts=self._clock(),
                date=day.isoformat(),
                contract_symbol=pos.contract_symbol,
                fill_price=fill.price,
                proceeds_usd=round(
                    fill.price * CONTRACT_MULTIPLIER * fill.qty, 2
                ),
                pnl_usd=pnl,
            )
            state["position"] = None
            return

        if state_pos is not None:
            self._pos = state_pos
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

    # ------------------------------------------------- live-frontier gate
    def _bar_is_live(self, bar: Bar) -> bool:
        """Same rule as the shares runner: a bar is LIVE iff it
        completed after this session's live frontier; no frontier
        (offline replay / tests) = every bar is live."""
        if self._live_since is None:
            return True
        return bar.ts + timedelta(minutes=1) > self._live_since

    def _place_entry_order(self, bar: Bar, contract_symbol: str, side: str) -> Fill:
        """THE entry-order path — the live-frontier gate lives here
        so no caller can bypass it (shares-runner pattern). Exits
        never come through here."""
        if not self._bar_is_live(bar):
            raise RuntimeError(
                "live-frontier gate: refusing to place an entry "
                f"order for historical bar {bar.ts.isoformat()} "
                f"(live_since={self._live_since})"
            )
        return self.broker.place_market(
            contract_symbol, CONTRACTS_PER_TRADE, side
        )

    # -------------------------------------------------------------- exits
    def _exit_position(
        self, reason: str, state: dict, summary: dict, bar: Bar,
        managed_by: Optional[str] = None,
    ) -> bool:
        """Sell the held contract at market for ``reason`` (the
        strategy's exit reason, or DATA_END for the enforced
        flatten). Falls back once to close_position; on total
        failure logs exit_failure_critical, marks the exit pending
        for retry, and returns False. Books ACTUAL premiums."""
        pos = self._pos
        assert pos is not None
        day = bar.ts.date()
        self._set_cid(day, f"exit-{reason.lower()}")
        fill: Optional[Fill] = None
        first_error: Optional[Exception] = None
        try:
            fill = self.broker.place_market(
                pos.contract_symbol, pos.qty_contracts, "sell"
            )
        except Exception as exc:  # noqa: BLE001 - fallback decides next
            first_error = exc
        if fill is None:
            self.log.emit(
                "exit_order_failed",
                ts=bar.ts,
                date=bar.ts.date().isoformat(),
                contract_symbol=pos.contract_symbol,
                exit_reason=reason,
                error=str(first_error),
                fallback="close_position",
            )
            try:
                fill = self.broker.close_position(pos.contract_symbol)
            except Exception as exc:  # noqa: BLE001 - critical, retried later
                self.log.emit(
                    "exit_failure_critical",
                    ts=bar.ts,
                    date=bar.ts.date().isoformat(),
                    contract_symbol=pos.contract_symbol,
                    exit_reason=reason,
                    order_error=str(first_error),
                    close_error=str(exc),
                )
                self._exit_pending_reason = reason
                return False
        pnl = self._book_exit(fill)
        self._apply_pnl(state, summary, pnl)
        fully_closed = self._pos is None
        if fully_closed:
            self._exit_pending_reason = None
            self._phase = PHASE_DONE
        summary["trades"] += 1 if fully_closed else 0
        self.log.emit(
            "flatten_enforced" if reason == "DATA_END" else "trade_closed",
            ts=bar.ts,
            date=bar.ts.date().isoformat(),
            direction=pos.direction,
            option_type=pos.option_type,
            contract_symbol=pos.contract_symbol,
            exit_reason=reason,
            entry_premium_usd=pos.entry_premium_usd,
            fill_exit_premium_usd=fill.price,
            exit_proceeds_usd=round(
                fill.price * CONTRACT_MULTIPLIER * fill.qty, 2
            ),
            exit_order_id=fill.order_id,
            partial=not fully_closed,
            managed_by=managed_by,
            pnl_usd=pnl,
            overlay_equity_usd=summary["overlay_equity_usd"],
        )
        self._save(state)
        return True

    def _orphan_exit_reason(self, bar: Bar) -> Optional[str]:
        """Spec management for an adopted contract the strategy
        can't see (restart with a feed gap): levels are the
        UNDERLYING stop/target recorded at entry; flatten bar
        first, stop before target (stop wins ties) — the shares
        runner's _orphan_exit_reason semantics, verbatim."""
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

    # -------------------------------------------------------------- entries
    def _attempt_entry(self, bar: Bar, sp, day: date_type,
                       state: dict, summary: dict) -> None:
        """A fresh LIVE signal: select the contract (frozen rule),
        run the premium gate (frozen caps), and — only then — buy
        exactly 1 contract. Every refusal path logs its reason and
        ends the day's trading; the strategy's simulated trade
        continues to be observed (its close logs observe-only)."""
        try:
            chain = self.broker.contracts_for_selection(self.underlying, day)
        except AlpacaAPIError as exc:
            summary["no_trade_reason"] = "options_data_unavailable"
            self.log.emit(
                "options_data_unavailable",
                ts=bar.ts, date=day.isoformat(), stage="chain",
                error=str(exc),
            )
            self._phase = PHASE_DONE
            self._save(state)
            return
        contract = select_contract(chain, sp.direction, sp.entry_price, day)
        if contract is None:
            summary["no_trade_reason"] = "contract_unavailable"
            self.log.emit(
                "contract_unavailable",
                ts=bar.ts, date=day.isoformat(),
                direction=sp.direction,
                strategy_entry_price=sp.entry_price,
            )
            self._phase = PHASE_DONE
            self._save(state)
            return
        self.log.emit(
            "contract_selected",
            ts=bar.ts, date=day.isoformat(),
            contract_symbol=contract.symbol,
            option_type=contract.option_type,
            strike=contract.strike,
            expiry=contract.expiry.isoformat(),
            direction=sp.direction,
            signal_price=sp.entry_price,
        )
        summary["contract_symbol"] = contract.symbol
        try:
            quote = self.broker.fresh_quote(
                contract.symbol,
                now=self._clock(),
                max_staleness_minutes=self.max_quote_staleness_minutes,
            )
        except StaleQuoteError as exc:
            self._premium_skip(bar, day, state, summary, contract,
                               reason="stale_quote", detail=str(exc))
            return
        except QuoteError as exc:
            self._premium_skip(bar, day, state, summary, contract,
                               reason="quote_unavailable", detail=str(exc))
            return
        if quote.ask is None or quote.ask > self.premium_cap_usd:
            self._premium_skip(
                bar, day, state, summary, contract,
                reason="ask_above_cap",
                detail=f"ask {quote.ask} > cap {self.premium_cap_usd}",
                ask=quote.ask,
            )
            return
        self.log.emit(
            "premium_gate_passed",
            ts=bar.ts, date=day.isoformat(),
            contract_symbol=contract.symbol,
            ask=quote.ask, bid=quote.bid,
            quote_ts=quote.ts.isoformat() if quote.ts else None,
            cap_usd=self.premium_cap_usd,
        )
        self._set_cid(day, "entry")
        fill = self._place_entry_order(bar, contract.symbol, "buy")
        self._pos = OptionsPosition(
            contract_symbol=contract.symbol,
            option_type=contract.option_type,
            direction=sp.direction,
            expiry=contract.expiry.isoformat(),
            strike=contract.strike,
            qty_contracts=fill.qty,
            entry_premium_usd=fill.price,
            underlying_entry_price=sp.entry_price,
            stop=sp.stop,
            target=sp.target,
            entry_time=sp.entry_time,
            entry_order_id=fill.order_id,
        )
        summary["premium_paid_usd"] = self._pos.entry_cost_usd
        self.log.emit(
            "order_filled",
            ts=bar.ts, date=day.isoformat(), kind="entry",
            contract_symbol=contract.symbol,
            order_id=fill.order_id,
            fill_premium_usd=fill.price,
            qty_contracts=fill.qty,
            cost_usd=self._pos.entry_cost_usd,
            max_loss_usd=self._pos.entry_cost_usd,
        )
        self._phase = PHASE_IN_POSITION
        self._save(state)

    def _premium_skip(self, bar: Bar, day: date_type, state: dict,
                      summary: dict, contract, *, reason: str,
                      detail: str, ask: Optional[float] = None) -> None:
        summary["premium_skips"] += 1
        summary["no_trade_reason"] = "premium_skip"
        self.log.emit(
            "premium_skip",
            ts=bar.ts, date=day.isoformat(),
            contract_symbol=contract.symbol,
            reason=reason, detail=detail, ask=ask,
            cap_usd=self.premium_cap_usd,
            max_staleness_minutes=self.max_quote_staleness_minutes,
        )
        self._phase = PHASE_DONE
        self._save(state)

    # ----------------------------------------------------------------- day
    def run_day(
        self,
        day: date_type,
        bars: Iterable[Bar],
        *,
        live_since: Optional[datetime] = None,
    ) -> dict:
        """Run one ET trading day over completed RTH 1-minute SPY
        bars. ``live_since`` is the live frontier (see the module
        docstring): catch-up signals are logged as missed days and
        never traded. None = every bar live (replay/tests)."""
        state = self.store.load() or self._fresh_state(day)
        if state.get("date") != day.isoformat() and not state.get("position"):
            state["date"] = day.isoformat()
        self._pos = None
        self._phase = PHASE_IDLE
        self._exit_pending_reason = None
        summary = {
            "date": day.isoformat(),
            "variant": "orb15_options",
            "halted": False,
            "trades": 0,
            "realized_pnl_usd": 0.0,
            "overlay_equity_usd": state.get(
                "overlay_equity_usd", self.overlay_start_usd
            ),
            "premium_skips": 0,
            "skipped_for_size": 0,
            "orders_blocked_kill_switch": 0,
            "missed_day": False,
            "catchup_bars": 0,
            "live_since": live_since.isoformat() if live_since else None,
            "no_trade_reason": None,
            "contract_symbol": None,
            "premium_paid_usd": None,
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
            "options_runner_day_start",
            ts=self._clock(),
            date=day.isoformat(),
            enabled=self.enabled,
            live_since=live_since.isoformat() if live_since else None,
            feed_note=OPTIONS_FEED_NOTE,
            options_data_note=OPTIONS_DATA_NOTE,
        )
        try:
            self._reconcile(day, state)
        except ReconciliationHalt as exc:
            return self._halt(day, state, summary, str(exc))
        state["overlay_equity_usd"] = summary["overlay_equity_usd"] = round(
            self.overlay_start_usd
            + float(state.get("realized_pnl_total_usd") or 0.0),
            2,
        )
        self._live_since = live_since
        state["live_since"] = live_since.isoformat() if live_since else None
        state["catchup_bars"] = 0
        state["missed_day"] = None
        resumed = self._pos is not None
        self._phase = PHASE_IN_POSITION if resumed else PHASE_IDLE
        self._save(state)

        strat = STRATEGY_FACTORIES["orb15"](SPY_META)
        strat.start_day(day.isoformat())
        entry_seen = False
        entry_adopted = False
        strat_trades_seen = 0
        done_logged = False
        missed: Optional[dict] = None
        missed_logged = False
        missed_record: Optional[dict] = None
        frontier_reached = live_since is None
        catchup_bars = 0
        last_catchup_close: Optional[float] = None
        last_bar: Optional[Bar] = None

        def handle_exit_from_strategy(trade, bar) -> None:
            """The strategy closed its simulated trade on this bar:
            mirror it with a real market sell (if we hold one)."""
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
                    missed_logged = True
                    missed_record = self._log_missed_day(
                        day, missed, outcome="closed_in_replay", trade=trade
                    )
                    state["missed_day"] = missed_record
                    self._save(state)
                return
            self._exit_position(trade.exit_reason, state, summary, bar)

        for bar in bars:
            if not _is_rth(bar):
                continue
            last_bar = bar
            if not self._bar_is_live(bar):
                catchup_bars += 1
                last_catchup_close = bar.close
            elif not frontier_reached:
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

            # --- a pending exit from a failed attempt retries on
            # every later bar until it fills (F3 containment).
            if self._pos is not None and self._exit_pending_reason is not None:
                self._exit_position(
                    self._exit_pending_reason, state, summary, bar
                )

            # --- entry transition
            if strat.position is not None and not entry_seen:
                entry_seen = True
                sp = strat.position
                if self._pos is not None:
                    # Restart adoption: the strategy re-fired the
                    # signal behind the recorded contract. The
                    # underlying geometry must agree (float-tolerant:
                    # the strategy recomputes the target from the
                    # range, so exact bit equality is not expected).
                    def _close(a, b):
                        return abs(float(a) - float(b)) < 1e-6

                    if (
                        sp.direction != self._pos.direction
                        or not _close(
                            sp.entry_price, self._pos.underlying_entry_price
                        )
                        or not _close(sp.stop, self._pos.stop)
                        or not _close(sp.target, self._pos.target)
                    ):
                        return self._halt(
                            day, state, summary,
                            "restart rebuild mismatch: strategy "
                            f"re-fired {sp.direction} @ {sp.entry_price} "
                            f"(stop {sp.stop}, target {sp.target}) but "
                            f"state holds {self._pos.contract_symbol} "
                            f"from {self._pos.direction} @ "
                            f"{self._pos.underlying_entry_price} "
                            f"(stop {self._pos.stop}, "
                            f"target {self._pos.target})",
                        )
                    entry_adopted = True
                    self.log.emit(
                        "entry_adopted_after_restart",
                        ts=bar.ts,
                        date=day.isoformat(),
                        position=self._pos.to_state(),
                    )
                else:
                    if missed is not None or not self._bar_is_live(bar):
                        # LIVE-FRONTIER GATE (mirrors the shares
                        # runner): a catch-up signal is history.
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
                            shares_note=(
                                "strategy share count is record-only "
                                "here: options sizing is exactly 1 "
                                "contract"
                            ),
                        )
                        if not self.enabled:
                            summary["orders_blocked_kill_switch"] += 1
                            self.log.emit(
                                "order_blocked_kill_switch",
                                ts=bar.ts,
                                date=day.isoformat(),
                                direction=sp.direction,
                            )
                            self._phase = PHASE_DONE
                        else:
                            self._attempt_entry(bar, sp, day, state, summary)

            # --- orphan management: we hold a KNOWN contract the
            # strategy doesn't (restart with a feed gap). Manage it
            # from the recorded UNDERLYING levels, 15:55 flatten
            # included; engages only once no (re-)entry can fire.
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
                    self._exit_position(
                        reason, state, summary, bar,
                        managed_by="runner_orphan_levels",
                    )

            # --- skipped / void detection (once)
            if (
                not done_logged
                and strat.done
                and self._pos is None
                and not strat.trades
                and not entry_seen
                and summary["trades"] == 0
                and summary["premium_skips"] == 0
            ):
                done_logged = True
                if strat.skipped_for_size > 0:
                    summary["skipped_for_size"] = strat.skipped_for_size
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

        # --- catch-up trade never resolved inside the data
        if missed is not None and not missed_logged:
            missed_logged = True
            missed_record = self._log_missed_day(
                day,
                missed,
                outcome="open_at_end_of_data",
                mark_price=last_catchup_close,
            )

        # --- end of bars: a known contract still open is sold NOW.
        # Never carried overnight by choice (rule 20 handles the
        # leftover case if even this fails).
        if self._pos is not None:
            exit_bar = last_bar or Bar(
                ts=datetime.combine(day, SESSION_END, tzinfo=ET),
                open=0.0, high=0.0, low=0.0, close=0.0, volume=0.0,
            )
            self._exit_position("DATA_END", state, summary, exit_bar)

        if summary["no_trade_reason"] is None and summary["trades"] == 0:
            if getattr(strat, "skipped_for_size", 0) > 0:
                summary["skipped_for_size"] = strat.skipped_for_size
                summary["no_trade_reason"] = "skipped_for_size"
            elif entry_seen or resumed:
                summary["no_trade_reason"] = None
            elif getattr(strat, "_range_high", None) is None:
                summary["no_trade_reason"] = "no_opening_range_bars"
            else:
                summary["no_trade_reason"] = "no_breakout"
        if self._phase != PHASE_HALTED and self._pos is None:
            self._phase = PHASE_DONE
        state["catchup_bars"] = catchup_bars
        summary["catchup_bars"] = catchup_bars
        if missed_record is not None:
            state["missed_day"] = missed_record
        if self._pos is not None:
            summary["open_position_at_end"] = self._pos.contract_symbol
        self._save(state)
        summary["overlay_equity_usd"] = state["overlay_equity_usd"]
        self.log.emit("day_summary", ts=self._clock(), **summary)
        return summary

    def _log_missed_day(
        self,
        day: date_type,
        missed: dict,
        *,
        outcome: str,
        trade=None,
        mark_price: Optional[float] = None,
    ) -> dict:
        """Mirror of the shares runner's missed-day record: the
        replayed outcome is FOR THE RECORD ONLY (the strategy P&L
        fields are the SHARES model's, kept for schema parity) —
        nothing is booked to the options overlay."""
        fields = {"date": day.isoformat(), **missed, "outcome": outcome}
        if trade is not None:
            fields.update(
                exit_reason=trade.exit_reason,
                strategy_exit_price=trade.exit_price,
                strategy_pnl_usd=trade.pnl_usd,
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
        """Run forever (or ``max_days`` days): hours-gated exactly
        like the shares runner — ET weekdays 09:20–16:10 trigger a
        day run; a halted day stops the loop."""
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
        prog="alpaca.options_runner",
        description="ORB-15 OPTIONS variant forward test on the "
        "Alpaca PAPER account.",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="state + event-log directory "
        "(default: $ALPACA_OPTIONS_STATE_DIR or ./alpaca_options_state)",
    )
    args = parser.parse_args(argv)

    try:
        config = AlpacaConfig.from_env()
    except MissingCredentialsError as exc:
        print(f"REFUSING TO START: {exc}", file=sys.stderr)
        return 2
    client = OptionsClient(config)
    try:
        account = client.verify_paper()
    except (AlpacaAPIError, PaperOnlyError) as exc:
        print(f"REFUSING TO START: paper account check failed: {exc}",
              file=sys.stderr)
        return 2

    state_dir = Path(
        args.state_dir
        or os.environ.get(ENV_OPTIONS_STATE_DIR)
        or "alpaca_options_state"
    )
    lock = InstanceLock(state_dir / "options_runner.lock")
    if not lock.acquire():
        # F7 pattern: a second instance refuses CLEANLY (no crash
        # loop) rather than double-trading the account.
        print(
            "REFUSING TO START: another options-runner instance "
            f"holds {lock.path}; exiting cleanly.",
            file=sys.stderr,
        )
        return 0
    try:
        enabled = runner_enabled()
        broker = OptionsBroker(client)
        log = EventLog(state_dir / "events.jsonl")
        store = StateStore(state_dir / "runner_state.json")
        runner = Orb15OptionsRunner(
            broker, enabled=enabled, log=log, store=store
        )
        source = AlpacaBarSource(client, clock=lambda: datetime.now(ET))

        print(f"ORB-15 OPTIONS variant — Alpaca PAPER forward test "
              f"($500 overlay) — account "
              f"{account.get('account_number', '?')} status "
              f"{account.get('status', '?')}")
        print(f"orders enabled (ALPACA_RUNNER_ENABLED): {enabled}")
        print(f"OPTIONS DATA NOTE: {OPTIONS_DATA_NOTE}")
        print(f"FEED NOTE: {OPTIONS_FEED_NOTE}")
        log.emit("runner_process_start", enabled=enabled,
                 variant="orb15_options",
                 account_status=account.get("status"))
        summary = runner.serve(source.bars_for_day)
        if summary and summary.get("halted"):
            print(f"HALTED: {summary.get('halt_reason')}", file=sys.stderr)
            return 3
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
