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
    exit_reason: str = ""  # TARGET | STOP | STOP_BE | EOD_FLATTEN | DATA_END
    pnl_usd: float = 0.0
    r_multiple: float = 0.0
    # Partial-exit leg (V3 "partials" variant only; zeroes/empty otherwise).
    # The exit_* fields above then describe the REMAINDER leg, pnl_usd is
    # the two legs combined, and r_multiple is measured against the trade's
    # INITIAL risk dollars.
    partial_units: int = 0
    partial_exit_time: str = ""
    partial_exit_price: float = 0.0
    partial_pnl_usd: float = 0.0

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
            "partial_units": self.partial_units,
            "partial_exit_time": self.partial_exit_time,
            "partial_exit_price": round(self.partial_exit_price, 4),
            "partial_pnl_usd": round(self.partial_pnl_usd, 2),
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
        self._fc_open: Optional[float] = None   # first-candle body (V4 trend)
        self._fc_close: Optional[float] = None
        self._recent: list[Bar] = []
        self._day_bars: list[Bar] = []          # all RTH bars today (V1 ATR)
        # V3 partials bookkeeping (inert unless cfg["partials"] is set).
        self._partial_done: bool = False
        self._remaining: int = 0                # units still open
        self._partial_level: float = 0.0        # +1.5R price
        self._eff_stop: Optional[float] = None  # stop after any BE move

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
        self._fc_open = None
        self._fc_close = None
        self._recent = []
        self._day_bars = []
        self._partial_done = False
        self._remaining = 0
        self._partial_level = 0.0
        self._eff_stop = None

    # ------------------------------------------------------------------ API
    def on_bar(self, bar: Bar) -> Optional[Trade]:
        """Feed one 1-minute bar. Returns a Trade when a position closes."""
        t = bar.ts.time()

        if self.state == DONE:
            return None

        fc_start = self.cfg["first_candle_start"]
        if t >= fc_start:
            # RTH bars of the current day, in order (V1's ATR window reads
            # this list; it is not used for anything else).
            self._day_bars.append(bar)

        if self.state == WAIT_FIRST_CANDLE:
            fc_end = self.cfg["first_candle_end"]
            if fc_start <= t < fc_end:
                if self._fc_open is None:
                    self._fc_open = bar.open
                self._fc_close = bar.close
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
            # V2 (fc_filter): the runner precomputes a per-day limit from
            # PRIOR days' first-candle ranges; a wider first candle today
            # voids the whole day. Absent key -> filter off (baseline).
            limit = self.cfg.get("fc_range_limit")
            if limit is not None and (self._fc_high - self._fc_low) > float(limit):
                self.state = DONE
                return None
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
            if not (b1.close > self.level_high or b2.close > self.level_high):
                return
            direction = "long"
        else:  # bearish
            if not (b1.close < self.level_low or b2.close < self.level_low):
                return
            direction = "short"
        # V4 (trend): only setups in the direction of the first candle's
        # body (close vs open of the 09:30-09:35 candle; equal -> both).
        if self.cfg.get("trend_filter") and not self._trend_allows(direction):
            return
        # V1 (min_fvg): the gap must be at least the configured multiple
        # (0.5) of ATR(14) over today's RTH bars before the displacement
        # candle. Fewer than 5 such bars -> the setup is invalid.
        mult = self.cfg.get("min_fvg_atr_mult")
        if mult is not None:
            atr = self._atr_before(b1.ts)
            if atr is None or (fvg.top - fvg.bottom) < float(mult) * atr:
                return
        self.pending = {"direction": direction, "fvg": fvg}
        self.state = WAIT_RETEST

    def _trend_allows(self, direction: str) -> bool:
        """V4 gate: first-candle body direction (both allowed if flat)."""
        if self._fc_open is None or self._fc_close is None:
            return True
        if self._fc_close > self._fc_open:
            return direction == "long"
        if self._fc_close < self._fc_open:
            return direction == "short"
        return True

    def _atr_before(self, ts) -> Optional[float]:
        """ATR(14) of today's RTH bars strictly before timestamp `ts`.

        True ranges use the previous bar's close (the day's first bar
        falls back to its own high-low). With fewer than 14 bars the
        average uses what exists; with fewer than 5 the setup window is
        too young and None is returned (setup invalid, per the V1 spec).
        """
        bars = [b for b in self._day_bars if b.ts < ts]
        if len(bars) < 5:
            return None
        trs: list[float] = []
        prev_close: Optional[float] = None
        for b in bars:
            if prev_close is None:
                tr = b.high - b.low
            else:
                tr = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
            trs.append(tr)
            prev_close = b.close
        window = trs[-14:]
        return sum(window) / len(window)

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
        # V3 bookkeeping: full size open, stop at the entry stop, partial
        # trigger at +1.5R. (Tracked always; only used when partials on.)
        self._partial_done = False
        self._remaining = units
        self._eff_stop = stop
        if direction == "long":
            self._partial_level = entry + 1.5 * risk_distance
        else:
            self._partial_level = entry - 1.5 * risk_distance
        self.pending = None
        self.state = IN_TRADE  # one-trade-per-day lock: no path back to WAIT_BREAK

    def _process_in_trade(self, bar: Bar) -> Optional[Trade]:
        pos = self.position
        # Flatten at flatten_time, at that bar's open.
        if bar.ts.time() >= self.cfg["flatten_time"]:
            return self._exit(bar.ts, bar.open, "EOD_FLATTEN")
        if self.cfg.get("partials"):
            return self._process_in_trade_partials(bar)
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

    def _process_in_trade_partials(self, bar: Bar) -> Optional[Trade]:
        """V3: at +1.5R bank half (floor) and move the stop to breakeven;
        the remainder runs to the original +3R target, the moved stop, or
        the 15:55 flatten (handled by the caller)."""
        pos = self.position
        stop = self._eff_stop
        long = pos.direction == "long"
        hit_stop = bar.low <= stop if long else bar.high >= stop
        hit_target = bar.high >= pos.target if long else bar.low <= pos.target
        if not self._partial_done:
            # The original stop keeps priority on the trigger bar (same
            # conservative convention as the baseline stop-vs-target rule):
            # a bar spanning both the stop and +1.5R is a full stop-out.
            if hit_stop:
                return self._exit(bar.ts, stop, "STOP")
            hit_partial = (
                bar.high >= self._partial_level if long
                else bar.low <= self._partial_level
            )
            if not hit_partial:
                return None
            self._take_partial(bar)
            # Reaching +3R requires crossing +1.5R first, so if this same
            # bar also reached the target, the remainder exits there.
            if self._remaining > 0 and hit_target:
                return self._exit(bar.ts, pos.target, "TARGET")
            return None
        # Remainder phase: stop (possibly at breakeven) first, then target.
        if hit_stop:
            reason = "STOP_BE" if stop == pos.entry_price else "STOP"
            return self._exit(bar.ts, stop, reason)
        if hit_target:
            return self._exit(bar.ts, pos.target, "TARGET")
        return None

    def _take_partial(self, bar: Bar) -> None:
        """Execute the +1.5R partial leg and move the stop to breakeven."""
        pos = self.position
        self._partial_done = True
        take = pos.units // 2 if pos.units >= 2 else 0
        if take > 0:
            price = self._partial_level
            if pos.direction == "long":
                leg = (price - pos.entry_price) * take * self._point_value
            else:
                leg = (pos.entry_price - price) * take * self._point_value
            pos.partial_units = take
            pos.partial_exit_time = bar.ts.strftime("%H:%M")
            pos.partial_exit_price = price
            pos.partial_pnl_usd = leg
            self._remaining = pos.units - take
        self._eff_stop = pos.entry_price  # breakeven for the remainder

    def _exit(self, ts: datetime, price: float, reason: str) -> Trade:
        pos = self.position
        pos.exit_time = ts.strftime("%H:%M")
        pos.exit_price = price
        pos.exit_reason = reason
        # The exit leg covers only the units still open (all of them,
        # unless a V3 partial already banked some); R is always measured
        # against the INITIAL risk dollars.
        if pos.direction == "long":
            leg = (price - pos.entry_price) * self._remaining * self._point_value
        else:
            leg = (pos.entry_price - price) * self._remaining * self._point_value
        pos.pnl_usd = pos.partial_pnl_usd + leg
        risked = pos.risk_distance * pos.units * self._point_value
        pos.r_multiple = pos.pnl_usd / risked if risked > 0 else 0.0
        self.trades.append(pos)
        self.position = None
        self.state = DONE
        return pos
