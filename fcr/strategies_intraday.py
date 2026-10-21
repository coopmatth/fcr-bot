"""Intraday strategy pack: day-state machines beside the FCR engine.

Each strategy consumes one day of 1-minute RTH bars (09:30 <= t < 16:00 ET)
through the same Bar feed the FCR engine uses, and emits fcr.engine.Trade
records, so trade rows are byte-compatible with the FCR backtester's
(results/trades_*.csv columns; the runner adds a friction_usd column).

Common spec (frozen; identical for every strategy in this module):
  * paper account $500 start, tracked across the run by the runner
  * risk budget $150/trade; futures units =
    min(5, floor(150 / (stop_pts x point_value))); SPY shares uncapped
    (floor(150 / stop_$)); 0 units -> the signal is counted as
    skipped-for-size and the day is over (the defining trigger has fired;
    there is no second bite at a wider stop)
  * at most ONE trade per day per strategy
  * entries fill at the trigger bar's CLOSE; management (stop/target)
    starts on the NEXT bar
  * a position still open at the 15:55 bar flattens at that bar's open
  * if one bar touches both stop and target, the stop wins (conservative)
  * exits fill AT the stop/target price (FCR convention; gaps through a
    level do not improve or worsen the fill)
  * ATR = ATR(14) of today's RTH 1-min bars STRICTLY BEFORE the current
    bar (true ranges vs the previous close; fewer than 14 bars -> use
    what exists; fewer than 5 -> None, and ATR-gated logic cannot fire)
  * degenerate geometry (stop/target not strictly on the correct side of
    the entry) voids the signal and ends the day without a trade
  * STRESS HARNESS ONLY — entry_delay_bars (default 0; NOT part of any
    frozen strategy spec): when set on a DayStrategy, a validated
    signal is held instead of filled and is filled at the OPEN of the
    bar entry_delay_bars later, with the stop and target kept as the
    signal-time price levels; units are re-sized at the fill price.
    An open that has already crossed the stop or the target voids the
    trade (counted in lag_voided; the day is over) — the moment has
    passed. The fill bar is live exposure: stop/target management runs
    on it after the fill. A signal still pending when the day's data
    ends produces no trade.

Strategies:
  orb15 / orb30  opening-range breakout, first close beyond the range,
                 stop at the opposite edge, target 2R
  orb15_close    orb15 with NO profit target (Zarattini & Aziz style):
                 exits only at the stop or the 15:55 flatten; the trade
                 record's target field is NaN (blank in the CSV)
  pd_sweep       previous-day sweep (liquidity fade): fade the first
                 sweep of the prior RTH day's high/low once a bar closes
                 back inside; stop beyond the sweep extreme (+/- 1 tick),
                 target 2R
  vwap_revert    close beyond the session VWAP +/- 2-sigma band between
                 10:30 and 14:30, then a close back inside -> fade it;
                 stop beyond the excursion extreme (+/- 1 tick),
                 target = VWAP at entry
  vwap_trend     trend declared at 10:30 (close vs VWAP + VWAP slope vs
                 10:00); first pullback touch of VWAP (+/- 0.05%) then a
                 close through the prior bar's high/low -> enter;
                 stop = VWAP at entry -/+ 0.5 x ATR; target 2R
  ib_break       first close beyond the 09:30-10:30 Initial Balance after
                 10:30 on a bar whose range >= 0.5 x ATR; stop at the IB
                 midpoint; target 2R
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date as date_type
from datetime import time
from typing import Optional

import pandas as pd

from .backtest import max_drawdown_usd
from .engine import Trade, compute_units
from .patterns import Bar

SESSION_START = time(9, 30)
SESSION_END = time(16, 0)
ENTRY_CUTOFF = time(12, 0)
FLATTEN_TIME = time(15, 55)

# Minute-of-day anchors (ET) used by the time-windowed logic.
M_SESSION_START = 9 * 60 + 30   # 570
M_CUTOFF = 12 * 60              # 720
M_IB_END = 10 * 60 + 30         # 630
M_VWAP_1000 = 10 * 60           # 600
M_REVERT_END = 14 * 60 + 30     # 870


def _minute(bar: Bar) -> int:
    return bar.ts.hour * 60 + bar.ts.minute


@dataclass(frozen=True)
class SymbolMeta:
    """Everything a strategy needs to know about the traded instrument."""

    symbol: str                       # traded-as symbol, e.g. "MGC"
    point_value_usd: float            # $ per 1.00 price point per unit
    tick_size: float                  # minimum price increment
    max_units: Optional[int]          # contract cap (None = uncapped shares)
    friction_per_unit_usd: float      # round-trip friction per unit
    risk_budget_usd: float = 150.0
    instrument_type: str = "futures"


def atr14(bars: list[Bar]) -> Optional[float]:
    """ATR(14) over the given bars (same semantics as the FCR engine's V1
    window): true ranges against the previous close, averaged over the
    last 14; fewer bars -> average what exists; fewer than 5 -> None."""
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


class SessionVWAP:
    """Cumulative session VWAP and volume-weighted sigma (anchored bands).

    update(bar) folds one bar in and returns (vwap, sigma); both are
    None until some volume has printed. sigma is the volume-weighted
    standard deviation of typical price around the CURRENT vwap, via
    var = E[TP^2] - VWAP^2 (algebraically the anchored-stdev formula).
    """

    def __init__(self) -> None:
        self._cum_pv = 0.0    # sum V * TP
        self._cum_p2v = 0.0   # sum V * TP^2
        self._cum_v = 0.0
        self.vwap: Optional[float] = None
        self.sigma: Optional[float] = None

    def update(self, bar: Bar) -> tuple[Optional[float], Optional[float]]:
        tp = (bar.high + bar.low + bar.close) / 3.0
        v = float(bar.volume)
        if v > 0:
            self._cum_pv += tp * v
            self._cum_p2v += tp * tp * v
            self._cum_v += v
        if self._cum_v <= 0:
            self.vwap = None
            self.sigma = None
        else:
            self.vwap = self._cum_pv / self._cum_v
            var = self._cum_p2v / self._cum_v - self.vwap * self.vwap
            self.sigma = math.sqrt(var) if var > 0 else 0.0
        return self.vwap, self.sigma


class DayStrategy:
    """Base class: shared position management + sizing + bookkeeping.

    Subclasses implement _scan(bar, prev_bar) (pre-entry signal logic,
    called once per bar while flat) and optionally _observe(bar) (called
    for every bar before anything else) and _reset(prev_high, prev_low).
    """

    name = "base"

    def __init__(self, meta: SymbolMeta):
        self.meta = meta
        self.date: Optional[str] = None
        self.trades: list[Trade] = []
        self.skipped_for_size = 0
        self.position: Optional[Trade] = None
        self._done = False
        self._day_bars: list[Bar] = []
        self._prev_bar: Optional[Bar] = None
        # Execution-lag stress harness (see module docstring). 0 = the
        # frozen spec: fill at the signal bar's close.
        self.entry_delay_bars = 0
        self._pending: Optional[dict] = None
        self.lag_voided = 0

    # -------------------------------------------------------------- API
    def start_day(
        self,
        date: str,
        prev_high: Optional[float] = None,
        prev_low: Optional[float] = None,
    ) -> None:
        self.date = date
        self.trades = []
        self.skipped_for_size = 0
        self.position = None
        self._done = False
        self._day_bars = []
        self._prev_bar = None
        self._pending = None
        self.lag_voided = 0
        self._reset(prev_high, prev_low)

    @property
    def done(self) -> bool:
        return self._done

    def on_bar(self, bar: Bar) -> Optional[Trade]:
        """Feed one RTH 1-minute bar. Returns a Trade when one closes."""
        prev = self._prev_bar
        self._prev_bar = bar
        if self._done:
            return None
        self._day_bars.append(bar)
        self._observe(bar)
        if self.position is not None:
            return self._manage(bar)
        if self._pending is not None:
            self._pending["bars_left"] -= 1
            if self._pending["bars_left"] > 0:
                return None
            self._fill_pending(bar)
            if self.position is not None:
                # Filled at this bar's open: the rest of the bar is
                # live exposure, so management runs on it.
                return self._manage(bar)
            return None
        self._scan(bar, prev)
        return None

    def force_close(self, ts, price: float, reason: str = "DATA_END") -> Optional[Trade]:
        """Close any open position at an arbitrary price (end of data)."""
        if self.position is not None:
            return self._exit(ts, price, reason)
        return None

    # -------------------------------------------------------- subclass hooks
    def _reset(self, prev_high: Optional[float], prev_low: Optional[float]) -> None:
        pass

    def _observe(self, bar: Bar) -> None:
        pass

    def _scan(self, bar: Bar, prev: Optional[Bar]) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------- internals
    def _atr_before_current(self) -> Optional[float]:
        """ATR over today's bars strictly before the current bar."""
        return atr14(self._day_bars[:-1])

    def _try_enter(
        self, bar: Bar, direction: str, stop: float, target: Optional[float]
    ) -> bool:
        """Open a position at the bar's close with shared sizing rules.

        target=None means NO profit target (orb15_close): the stored
        target is NaN and the position exits only at the stop, the 15:55
        flatten, or end of data. Returns True when a position opened.
        Degenerate geometry (stop, and target when present, not strictly
        bracketing the entry) voids the signal; a stop too wide for the
        risk budget counts as skipped-for-size. Either failure ends the
        day: the strategy's one signal has fired.
        """
        entry = bar.close
        if direction == "long":
            valid = stop < entry and (target is None or entry < target)
        else:
            valid = stop > entry and (target is None or entry > target)
        if not valid:
            self._done = True
            return False
        if self.entry_delay_bars > 0:
            # Stress harness: hold the validated signal; the fill
            # happens at a later bar's open with these same levels.
            self._pending = {
                "direction": direction,
                "stop": stop,
                "target": target,
                "bars_left": self.entry_delay_bars,
            }
            return True
        risk_distance = abs(entry - stop)
        units = compute_units(
            self.meta.risk_budget_usd,
            risk_distance,
            point_value_usd=self.meta.point_value_usd,
            max_units=self.meta.max_units,
        )
        if units <= 0:
            self.skipped_for_size += 1
            self._done = True
            return False
        self.position = Trade(
            date=self.date or bar.ts.date().isoformat(),
            direction=direction,
            entry_time=bar.ts.strftime("%H:%M"),
            entry_price=entry,
            stop=stop,
            target=float("nan") if target is None else target,
            units=units,
            risk_distance=risk_distance,
        )
        return True

    def _fill_pending(self, bar: Bar) -> None:
        """Fill a held signal at this bar's open (execution-lag stress).

        Stop/target stay at the signal-time levels; units are sized
        off the fill price. An open already beyond the stop or the
        target voids the trade; a fill too wide for the risk budget
        counts as skipped-for-size. Either failure ends the day.
        """
        pending = self._pending
        self._pending = None
        direction = pending["direction"]
        stop = pending["stop"]
        target = pending["target"]
        entry = bar.open
        if direction == "long":
            crossed = entry <= stop or (target is not None and entry >= target)
        else:
            crossed = entry >= stop or (target is not None and entry <= target)
        if crossed:
            self.lag_voided += 1
            self._done = True
            return
        risk_distance = abs(entry - stop)
        units = compute_units(
            self.meta.risk_budget_usd,
            risk_distance,
            point_value_usd=self.meta.point_value_usd,
            max_units=self.meta.max_units,
        )
        if units <= 0:
            self.skipped_for_size += 1
            self._done = True
            return
        self.position = Trade(
            date=self.date or bar.ts.date().isoformat(),
            direction=direction,
            entry_time=bar.ts.strftime("%H:%M"),
            entry_price=entry,
            stop=stop,
            target=float("nan") if target is None else target,
            units=units,
            risk_distance=risk_distance,
        )

    def _manage(self, bar: Bar) -> Optional[Trade]:
        pos = self.position
        # Flatten at the 15:55 bar, at its open (before stop/target).
        if bar.ts.time() >= FLATTEN_TIME:
            return self._exit(bar.ts, bar.open, "EOD_FLATTEN")
        has_target = not (isinstance(pos.target, float) and math.isnan(pos.target))
        if pos.direction == "long":
            if bar.low <= pos.stop:      # stop first if a bar spans both
                return self._exit(bar.ts, pos.stop, "STOP")
            if has_target and bar.high >= pos.target:
                return self._exit(bar.ts, pos.target, "TARGET")
        else:
            if bar.high >= pos.stop:
                return self._exit(bar.ts, pos.stop, "STOP")
            if has_target and bar.low <= pos.target:
                return self._exit(bar.ts, pos.target, "TARGET")
        return None

    def _exit(self, ts, price: float, reason: str) -> Trade:
        pos = self.position
        pos.exit_time = ts.strftime("%H:%M")
        pos.exit_price = price
        pos.exit_reason = reason
        sign = 1.0 if pos.direction == "long" else -1.0
        pos.pnl_usd = sign * (price - pos.entry_price) * pos.units * self.meta.point_value_usd
        risked = pos.risk_distance * pos.units * self.meta.point_value_usd
        pos.r_multiple = pos.pnl_usd / risked if risked > 0 else 0.0
        self.trades.append(pos)
        self.position = None
        self._done = True
        return pos


# ------------------------------------------------------------- ORB family
class ORBStrategy(DayStrategy):
    """Opening-range breakout: range = high/low of the first N minutes
    (bars stamped 09:30 <= t < 09:30+N). First CLOSE beyond the range on
    a bar stamped before 12:00 enters at that close; stop = the opposite
    edge of the range; target = entry +/- 2R (unless _with_target is
    False — see ORBCloseStrategy)."""

    _with_target = True

    def __init__(self, meta: SymbolMeta, minutes: int):
        super().__init__(meta)
        self.minutes = minutes
        self.name = f"orb{minutes}"
        self._window_end = M_SESSION_START + minutes

    def _reset(self, prev_high, prev_low) -> None:
        self._range_high: Optional[float] = None
        self._range_low: Optional[float] = None
        self._range_done = False

    def _scan(self, bar: Bar, prev: Optional[Bar]) -> None:
        m = _minute(bar)
        if not self._range_done:
            if m < self._window_end:
                self._range_high = (
                    bar.high if self._range_high is None else max(self._range_high, bar.high)
                )
                self._range_low = (
                    bar.low if self._range_low is None else min(self._range_low, bar.low)
                )
                return
            self._range_done = True
        if self._range_high is None or self._range_low is None:
            self._done = True  # no bars inside the opening window today
            return
        if m >= M_CUTOFF:
            return
        if bar.close > self._range_high:
            entry = bar.close
            target = entry + 2.0 * (entry - self._range_low) if self._with_target else None
            self._try_enter(bar, "long", self._range_low, target)
        elif bar.close < self._range_low:
            entry = bar.close
            target = entry - 2.0 * (self._range_high - entry) if self._with_target else None
            self._try_enter(bar, "short", self._range_high, target)


class ORBCloseStrategy(ORBStrategy):
    """orb15 with NO profit target (Zarattini & Aziz style): the position
    exits only at the stop or the 15:55 flatten. This is the version with
    published validation (QQQ 2016-2023, net of commissions); it sits in
    the matrix next to the 2R version for direct comparison."""

    _with_target = False

    def __init__(self, meta: SymbolMeta):
        super().__init__(meta, minutes=15)
        self.name = "orb15_close"


# ------------------------------------------------------- previous-day sweep
class PDSweepStrategy(DayStrategy):
    """Previous-day sweep (liquidity fade).

    A bar printing beyond the prior RTH day's high (low) arms the short
    (long) fade and the excursion extreme is tracked; the first bar that
    then CLOSES back inside the prior range enters at its close. The
    arming bar itself can also be the trigger bar (a one-bar sweep).
    Stop = excursion extreme +/- 1 tick; target = 2R. Only the first
    completed trigger of the day counts; entries only on bars stamped
    before 12:00.
    """

    name = "pd_sweep"

    def _reset(self, prev_high, prev_low) -> None:
        self._pdh = prev_high
        self._pdl = prev_low
        self._armed_short = False
        self._armed_long = False
        self._extreme_high: Optional[float] = None
        self._extreme_low: Optional[float] = None

    def _scan(self, bar: Bar, prev: Optional[Bar]) -> None:
        if self._pdh is None or self._pdl is None:
            return  # first day of the dataset: no prior levels, no trades
        if _minute(bar) >= M_CUTOFF:
            return
        if bar.high > self._pdh:
            self._armed_short = True
            self._extreme_high = (
                bar.high if self._extreme_high is None else max(self._extreme_high, bar.high)
            )
        if bar.low < self._pdl:
            self._armed_long = True
            self._extreme_low = (
                bar.low if self._extreme_low is None else min(self._extreme_low, bar.low)
            )
        tick = self.meta.tick_size
        if self._armed_short and bar.close < self._pdh:
            stop = self._extreme_high + tick
            entry = bar.close
            self._try_enter(bar, "short", stop, entry - 2.0 * (stop - entry))
        elif self._armed_long and bar.close > self._pdl:
            stop = self._extreme_low - tick
            entry = bar.close
            self._try_enter(bar, "long", stop, entry + 2.0 * (entry - stop))


# ------------------------------------------------------------ VWAP reversion
class VWAPRevertStrategy(DayStrategy):
    """Fade a close back inside the session VWAP +/- 2-sigma bands.

    Only bars stamped 10:30 <= t < 14:30 participate: a close beyond a
    band arms the fade on that side (excursion extreme tracked), and a
    LATER close back inside the bands enters at that close. A close that
    blows straight through to beyond the OPPOSITE band voids the
    excursion and arms the other side instead. Stop = excursion extreme
    +/- 1 tick; target = the VWAP value at entry (fixed). Exits only on
    stop / target / 15:55 flatten. A day whose session volume is zero
    throughout never defines bands and never trades.
    """

    name = "vwap_revert"

    def _reset(self, prev_high, prev_low) -> None:
        self._vwap = SessionVWAP()
        self._excursion: Optional[str] = None   # 'short' | 'long' (fade side)
        self._extreme: Optional[float] = None

    def _observe(self, bar: Bar) -> None:
        self._vwap.update(bar)

    def _scan(self, bar: Bar, prev: Optional[Bar]) -> None:
        m = _minute(bar)
        if m < M_IB_END or m >= M_REVERT_END:
            return
        vwap, sigma = self._vwap.vwap, self._vwap.sigma
        if vwap is None or sigma is None:
            return
        upper = vwap + 2.0 * sigma
        lower = vwap - 2.0 * sigma
        tick = self.meta.tick_size
        if self._excursion is None:
            if bar.close > upper:
                self._excursion = "short"
                self._extreme = bar.high
            elif bar.close < lower:
                self._excursion = "long"
                self._extreme = bar.low
            return
        if self._excursion == "short":
            self._extreme = max(self._extreme, bar.high)
            if bar.close <= upper:
                if bar.close >= lower:
                    self._try_enter(bar, "short", self._extreme + tick, vwap)
                else:  # blew through both bands: arm the other side
                    self._excursion = "long"
                    self._extreme = bar.low
        else:  # excursion == 'long'
            self._extreme = min(self._extreme, bar.low)
            if bar.close >= lower:
                if bar.close <= upper:
                    self._try_enter(bar, "long", self._extreme - tick, vwap)
                else:
                    self._excursion = "short"
                    self._extreme = bar.high


# ------------------------------------------------------------ VWAP trend day
class VWAPTrendStrategy(DayStrategy):
    """VWAP trend-pullback continuation.

    At 10:30 (using bars stamped < 10:30): trend-up if the last close is
    above VWAP and VWAP(through 10:29) > VWAP(through 09:59); trend-down
    mirrored; otherwise no trades today. After declaration, the first
    bar whose low (longs) comes within +/-0.05% of the running VWAP
    without breaking below VWAP x 0.9995 marks the pullback touch; the
    first LATER bar closing above the previous bar's high enters long at
    its close (mirror for shorts, using the bar's high and a close below
    the previous bar's low). Stop = VWAP at entry -/+ 0.5 x ATR;
    target = 2R. Entries only on bars stamped before 12:00.
    """

    name = "vwap_trend"

    def _reset(self, prev_high, prev_low) -> None:
        self._vwap = SessionVWAP()
        self._vwap_1000: Optional[float] = None
        self._pre_vwap: Optional[float] = None
        self._pre_close: Optional[float] = None
        self._declared = False
        self._trend: Optional[str] = None
        self._touched = False

    def _observe(self, bar: Bar) -> None:
        self._vwap.update(bar)
        m = _minute(bar)
        if m < M_VWAP_1000:
            self._vwap_1000 = self._vwap.vwap
        if m < M_IB_END:
            self._pre_vwap = self._vwap.vwap
            self._pre_close = bar.close

    def _declare(self) -> None:
        self._declared = True
        if self._vwap_1000 is None or self._pre_vwap is None or self._pre_close is None:
            return
        if self._pre_close > self._pre_vwap and self._pre_vwap > self._vwap_1000:
            self._trend = "long"
        elif self._pre_close < self._pre_vwap and self._pre_vwap < self._vwap_1000:
            self._trend = "short"

    def _scan(self, bar: Bar, prev: Optional[Bar]) -> None:
        m = _minute(bar)
        if not self._declared:
            if m < M_IB_END:
                return
            self._declare()
        if self._trend is None or m >= M_CUTOFF:
            return
        vwap = self._vwap.vwap
        if vwap is None:
            return
        if self._trend == "long":
            if not self._touched:
                if vwap * 0.9995 <= bar.low <= vwap * 1.0005:
                    self._touched = True
                return
            if prev is not None and bar.close > prev.high:
                atr = self._atr_before_current()
                if atr is None:
                    return
                stop = vwap - 0.5 * atr
                entry = bar.close
                self._try_enter(bar, "long", stop, entry + 2.0 * (entry - stop))
        else:  # trend == 'short'
            if not self._touched:
                if vwap * 0.9995 <= bar.high <= vwap * 1.0005:
                    self._touched = True
                return
            if prev is not None and bar.close < prev.low:
                atr = self._atr_before_current()
                if atr is None:
                    return
                stop = vwap + 0.5 * atr
                entry = bar.close
                self._try_enter(bar, "short", stop, entry - 2.0 * (stop - entry))


# ------------------------------------------------------- initial balance break
class IBBreakStrategy(DayStrategy):
    """Initial Balance breakout: IB = high/low of 09:30-10:30. The first
    bar stamped >= 10:30 and < 12:00 whose CLOSE is beyond the IB and
    whose range is at least 0.5 x ATR enters at the close; stop = IB
    midpoint; target = entry +/- 2R."""

    name = "ib_break"

    def _reset(self, prev_high, prev_low) -> None:
        self._ib_high: Optional[float] = None
        self._ib_low: Optional[float] = None
        self._ib_done = False

    def _scan(self, bar: Bar, prev: Optional[Bar]) -> None:
        m = _minute(bar)
        if not self._ib_done:
            if m < M_IB_END:
                self._ib_high = (
                    bar.high if self._ib_high is None else max(self._ib_high, bar.high)
                )
                self._ib_low = (
                    bar.low if self._ib_low is None else min(self._ib_low, bar.low)
                )
                return
            self._ib_done = True
        if self._ib_high is None or self._ib_low is None:
            self._done = True  # no morning bars today: no IB, no trades
            return
        if m >= M_CUTOFF:
            return
        atr = self._atr_before_current()
        if atr is None:
            return
        if (bar.high - bar.low) < 0.5 * atr:
            return
        mid = (self._ib_high + self._ib_low) / 2.0
        if bar.close > self._ib_high:
            entry = bar.close
            self._try_enter(bar, "long", mid, entry + 2.0 * (entry - mid))
        elif bar.close < self._ib_low:
            entry = bar.close
            self._try_enter(bar, "short", mid, entry - 2.0 * (mid - entry))


STRATEGY_FACTORIES = {
    "orb15": lambda meta: ORBStrategy(meta, minutes=15),
    "orb30": lambda meta: ORBStrategy(meta, minutes=30),
    "orb15_close": ORBCloseStrategy,
    "pd_sweep": PDSweepStrategy,
    "vwap_revert": VWAPRevertStrategy,
    "vwap_trend": VWAPTrendStrategy,
    "ib_break": IBBreakStrategy,
}

STRATEGY_LABELS = {
    "orb15": "ORB 15-min opening range breakout (2R target)",
    "orb30": "ORB 30-min opening range breakout (2R target)",
    "orb15_close": "ORB 15-min opening range breakout (no target, close exit)",
    "pd_sweep": "Previous-day sweep (liquidity fade)",
    "vwap_revert": "VWAP +/-2-sigma reversion",
    "vwap_trend": "VWAP trend pullback",
    "ib_break": "Initial Balance breakout",
}


def run_intraday_backtest(
    df: pd.DataFrame,
    strategy_key: str,
    meta: SymbolMeta,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    starting_balance: float = 500.0,
    entry_delay_bars: int = 0,
) -> tuple[list[dict], int, dict]:
    """Run one intraday strategy over a bars DataFrame (load_csv shape).

    Days outside [start, end] (when given) are not traded or counted but
    still supply prior-day high/low context for pd_sweep. Equity starts
    at starting_balance on the first in-range day. Returns (trade rows,
    in-range trading days, stats); rows carry the FCR extended trade
    columns plus friction_usd, and stats adds the net-of-friction twins
    (net_usd_after_friction, ending_equity_usd_after_friction).

    entry_delay_bars (default 0 = frozen spec) is the execution-lag
    stress harness: validated signals fill that many bars later, at
    that bar's open, with signal-time stop/target levels (see the
    DayStrategy docstring). Stats then add lag_voided (signals whose
    delayed open had already crossed the stop or target).
    """
    factory = STRATEGY_FACTORIES[strategy_key]
    df = df.copy()
    df["_date"] = df["timestamp"].dt.date
    rows: list[dict] = []
    days = 0
    skipped = 0
    lag_voided_total = 0
    equity = float(starting_balance)
    equity_curve: list[dict] = [{"date": "start", "equity": round(equity, 2)}]
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None
    for day, day_df in df.groupby("_date"):
        session = day_df[
            (day_df["timestamp"].dt.time >= SESSION_START)
            & (day_df["timestamp"].dt.time < SESSION_END)
        ]
        if session.empty:
            continue
        day_high = float(session["high"].max())
        day_low = float(session["low"].min())
        in_range = (start is None or day >= start) and (end is None or day <= end)
        if in_range:
            days += 1
            strat = factory(meta)
            strat.entry_delay_bars = entry_delay_bars
            strat.start_day(day.isoformat(), prev_high=prev_high, prev_low=prev_low)
            last_bar: Optional[Bar] = None
            for ts, o, h, l, c, v in zip(
                session["timestamp"],
                session["open"],
                session["high"],
                session["low"],
                session["close"],
                session["volume"],
            ):
                last_bar = Bar(
                    ts=ts.to_pydatetime(),
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=float(v),
                )
                strat.on_bar(last_bar)
            if strat.position is not None and last_bar is not None:
                strat.force_close(last_bar.ts, last_bar.close, reason="DATA_END")
            skipped += strat.skipped_for_size
            lag_voided_total += strat.lag_voided
            for t in strat.trades:
                row = t.as_row()
                row["friction_usd"] = round(t.units * meta.friction_per_unit_usd, 2)
                rows.append(row)
                equity += t.pnl_usd
            equity_curve.append({"date": day.isoformat(), "equity": round(equity, 2)})
        prev_high, prev_low = day_high, day_low

    n = len(rows)
    pnls = [r["pnl_usd"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    net = round(sum(pnls), 2)
    friction_total = round(sum(r["friction_usd"] for r in rows), 2)
    stats = {
        "trading_days": days,
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4) if n else None,
        "net_usd": net,
        "net_r": round(sum(r["r_multiple"] for r in rows), 3),
        "starting_balance_usd": float(starting_balance),
        "ending_equity_usd": round(float(starting_balance) + net, 2),
        "max_drawdown_usd": max_drawdown_usd(equity_curve),
        "avg_units": round(sum(r["contracts"] for r in rows) / n, 2) if n else None,
        "skipped_for_size": skipped,
        "entry_delay_bars": entry_delay_bars,
        "lag_voided": lag_voided_total,
        "total_friction_usd": friction_total,
        "net_usd_after_friction": round(net - friction_total, 2),
        "ending_equity_usd_after_friction": round(
            float(starting_balance) + net - friction_total, 2
        ),
        "equity_curve": equity_curve,
    }
    return rows, days, stats
