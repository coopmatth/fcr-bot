"""First Candle Rule strategy engine: a bar-by-bar state machine.

States: WAIT_FIRST_CANDLE -> LEVELS_SET -> WAIT_BREAK -> WAIT_RETEST
        -> IN_TRADE -> DONE

LEVELS_SET is transient: it is entered the moment the first candle completes
(on the first bar at/after first_candle_end) and that same bar is then
processed for break detection as WAIT_BREAK.

The engine is pure Python (no pandas) and consumes one Bar at a time, so
the exact same code can drive the backtester and a future live-paper
runner. The native configuration is 1-minute bars; the backtester can
also feed 5-minute bars (patterns then form on 5-minute bars). PAPER
ONLY: the engine emits simulated Trade records; it never talks to a
broker.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .patterns import Bar, detect_fvg, is_bearish_engulfing, is_bullish_engulfing

WAIT_FIRST_CANDLE = "WAIT_FIRST_CANDLE"
LEVELS_SET = "LEVELS_SET"
WAIT_BREAK = "WAIT_BREAK"
WAIT_RETEST = "WAIT_RETEST"
IN_TRADE = "IN_TRADE"
DONE = "DONE"


def compute_units(
    risk_budget_usd: float,
    risk_distance: float,
    point_value_usd: float = 1.0,
    max_units: int | None = None,
) -> int:
    """Whole units (contracts or shares) affordable for a dollar risk budget.

    One unit risks risk_distance * point_value_usd dollars, so the count is
    floor(budget / (distance * point_value)), capped at max_units when set.
    For equities point_value_usd is 1.0 and a unit is one share; for MNQ
    futures it is 2.0 ($2 per index point) and a unit is one contract.
    A result of 0 means the stop is too wide for the budget: skip the trade.
    """
    if risk_distance <= 0 or point_value_usd <= 0:
        return 0
    units = int(risk_budget_usd // (risk_distance * point_value_usd))
    if max_units is not None:
        units = min(units, int(max_units))
    return max(units, 0)


def position_size(risk_usd: float, risk_distance: float) -> int:
    """Legacy equity helper: whole shares for a dollar risk and stop distance."""
    return compute_units(risk_usd, risk_distance, point_value_usd=1.0)


def compute_target(direction: str, entry: float, stop: float, rr: float) -> float:
    """Fixed reward:risk target: entry +/- rr * |entry - stop|."""
    risk_distance = abs(entry - stop)
    if direction == "long":
        return entry + rr * risk_distance
    return entry - rr * risk_distance


@dataclass
class Trade:
    date: str
    direction: str  # 'long' or 'short'
    entry_time: str  # HH:MM (ET)
    entry_price: float
    stop: float
    target: float
    units: int  # contracts (futures) or shares (equity)
    risk_distance: float
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""  # TARGET | STOP | EOD_FLATTEN | DATA_END
    pnl_usd: float = 0.0
    r_multiple: float = 0.0

    def as_row(self) -> dict:
        return {
            "date": self.date,
            "direction": self.direction,
            "entry_time": self.entry_time,
            "entry_price": round(self.entry_price, 4),
            "stop": round(self.stop, 4),
            "target": round(self.target, 4),
            "exit_time": self.exit_time,
            "exit_price": round(self.exit_price, 4),
            "exit_reason": self.exit_reason,
            "contracts": self.units,
            "pnl_usd": round(self.pnl_usd, 2),
            "r_multiple": round(self.r_multiple, 3),
        }


class FCREngine:
    def __init__(self, config: dict):
        self.cfg = config
        self._point_value = float(config.get("point_value_usd", 1.0))
        self.date: Optional[str] = None
        self.state = WAIT_FIRST_CANDLE
        self.level_high: Optional[float] = None
        self.level_low: Optional[float] = None
        self.trades: list[Trade] = []
        self.skipped_for_size: int = 0  # entries discarded: stop too wide for budget
        self.position: Optional[Trade] = None
        self.pending: Optional[dict] = None  # {'direction': ..., 'fvg': FVG}
        self._fc_high: Optional[float] = None
        self._fc_low: Optional[float] = None
        self._recent: list[Bar] = []

    def start_day(self, date: str) -> None:
        self.date = date
        self.state = WAIT_FIRST_CANDLE
        self.level_high = None
        self.level_low = None
        self.trades = []
        self.skipped_for_size = 0
        self.position = None
        self.pending = None
        self._fc_high = None
        self._fc_low = None
        self._recent = []

    # ------------------------------------------------------------------ API
    def on_bar(self, bar: Bar) -> Optional[Trade]:
        """Feed one 1-minute bar. Returns a Trade when a position closes."""
        t = bar.ts.time()

        if self.state == DONE:
            return None

        if self.state == WAIT_FIRST_CANDLE:
            fc_start = self.cfg["first_candle_start"]
            fc_end = self.cfg["first_candle_end"]
            if fc_start <= t < fc_end:
                self._fc_high = bar.high if self._fc_high is None else max(self._fc_high, bar.high)
                self._fc_low = bar.low if self._fc_low is None else min(self._fc_low, bar.low)
                return None
            if t < fc_end:
                return None  # pre-open bar: ignore
            if self._fc_high is None:
                return None  # no first-candle data today: nothing to do
            # First candle complete -> levels are set (LEVELS_SET), and this
            # bar is also the first one eligible for break detection.
            self.level_high, self.level_low = self._fc_high, self._fc_low
            self.state = WAIT_BREAK
            self._recent = []
            # fall through and process this bar below

        prev_bar = self._recent[-1] if self._recent else None
        self._recent.append(bar)
        self._recent = self._recent[-3:]

        if self.state == IN_TRADE:
            return self._process_in_trade(bar)

        # Entry cutoff: a pending or potential setup dies at the cutoff.
        if self.state in (WAIT_BREAK, WAIT_RETEST) and t >= self.cfg["entry_cutoff"]:
            self.pending = None
            self.state = DONE
            return None

        if self.state == WAIT_BREAK:
            self._process_wait_break()
        elif self.state == WAIT_RETEST:
            self._process_wait_retest(bar, prev_bar)
        return None

    def force_close(self, ts: datetime, price: float, reason: str = "DATA_END") -> Optional[Trade]:
        """Close any open position at an arbitrary price (e.g., end of data)."""
        if self.state == IN_TRADE and self.position is not None:
            return self._exit(ts, price, reason)
        return None

    # -------------------------------------------------------------- states
    def _process_wait_break(self) -> None:
        if len(self._recent) < 3:
            return
        b0, b1, b2 = self._recent
        fvg = detect_fvg(b0, b1, b2)
        if fvg is None:
            return
        # The breaking move must actually cross the level: the displacement
        # candle (b1) or the confirming candle (b2) must CLOSE beyond it.
        if fvg.direction == "bullish":
            if b1.close > self.level_high or b2.close > self.level_high:
                self.pending = {"direction": "long", "fvg": fvg}
                self.state = WAIT_RETEST
        else:  # bearish
            if b1.close < self.level_low or b2.close < self.level_low:
                self.pending = {"direction": "short", "fvg": fvg}
                self.state = WAIT_RETEST

    def _process_wait_retest(self, bar: Bar, prev_bar: Optional[Bar]) -> None:
        fvg = self.pending["fvg"]
        direction = self.pending["direction"]

        # Invalidation (checked before any entry on the same bar):
        #  1) a close beyond the OPPOSITE first-candle level cancels the setup;
        #  2) a close through the far edge of the FVG (full fill) cancels it.
        cancelled = False
        if direction == "long":
            if bar.close < self.level_low or bar.close < fvg.bottom:
                cancelled = True
        else:
            if bar.close > self.level_high or bar.close > fvg.top:
                cancelled = True
        if cancelled:
            self.pending = None
            self.state = WAIT_BREAK
            return

        # Retest: the bar's range overlaps the FVG zone.
        overlaps = bar.low <= fvg.top and bar.high >= fvg.bottom
        if not overlaps or prev_bar is None:
            return

        # Entry trigger: engulfing candle in the setup direction.
        if direction == "long":
            triggered = is_bullish_engulfing(prev_bar, bar)
        else:
            triggered = is_bearish_engulfing(prev_bar, bar)
        if triggered:
            self._enter(bar, direction, fvg)

    def _enter(self, bar: Bar, direction: str, fvg) -> None:
        entry = bar.close
        stop = fvg.bottom if direction == "long" else fvg.top
        # Degenerate geometry (stop on the wrong side of entry) -> setup void.
        if (direction == "long" and stop >= entry) or (direction == "short" and stop <= entry):
            self.pending = None
            self.state = WAIT_BREAK
            return
        risk_distance = abs(entry - stop)
        units = compute_units(
            self.cfg.get("risk_budget_usd", self.cfg.get("risk_per_trade_usd", 0.0)),
            risk_distance,
            point_value_usd=float(self.cfg.get("point_value_usd", 1.0)),
            max_units=self.cfg.get("max_units"),
        )
        if units <= 0:  # stop wider than the whole risk budget: skip setup
            self.skipped_for_size += 1
            self.pending = None
            self.state = WAIT_BREAK
            return
        target = compute_target(direction, entry, stop, self.cfg["rr_target"])
        self.position = Trade(
            date=self.date or bar.ts.date().isoformat(),
            direction=direction,
            entry_time=bar.ts.strftime("%H:%M"),
            entry_price=entry,
            stop=stop,
            target=target,
            units=units,
            risk_distance=risk_distance,
        )
        self.pending = None
        self.state = IN_TRADE  # one-trade-per-day lock: no path back to WAIT_BREAK

    def _process_in_trade(self, bar: Bar) -> Optional[Trade]:
        pos = self.position
        # Flatten at flatten_time, at that bar's open.
        if bar.ts.time() >= self.cfg["flatten_time"]:
            return self._exit(bar.ts, bar.open, "EOD_FLATTEN")
        if pos.direction == "long":
            # If a bar touches both stop and target, assume the stop first
            # (conservative).
            if bar.low <= pos.stop:
                return self._exit(bar.ts, pos.stop, "STOP")
            if bar.high >= pos.target:
                return self._exit(bar.ts, pos.target, "TARGET")
        else:
            if bar.high >= pos.stop:
                return self._exit(bar.ts, pos.stop, "STOP")
            if bar.low <= pos.target:
                return self._exit(bar.ts, pos.target, "TARGET")
        return None

    def _exit(self, ts: datetime, price: float, reason: str) -> Trade:
        pos = self.position
        pos.exit_time = ts.strftime("%H:%M")
        pos.exit_price = price
        pos.exit_reason = reason
        if pos.direction == "long":
            pos.pnl_usd = (price - pos.entry_price) * pos.units * self._point_value
        else:
            pos.pnl_usd = (pos.entry_price - price) * pos.units * self._point_value
        risked = pos.risk_distance * pos.units * self._point_value
        pos.r_multiple = pos.pnl_usd / risked if risked > 0 else 0.0
        self.trades.append(pos)
        self.position = None
        self.state = DONE
        return pos
