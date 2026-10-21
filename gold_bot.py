#!/usr/bin/env python3
"""FCR gold bot — daily paper runner on the Tradovate DEMO API.

PAPER / DEMO ONLY. This runner trades exactly one thing — MGC (micro
gold) — with the strategy variant that passed the frozen 24-month
validation (results/gold24.json): the V0 baseline First Candle Rule,
no adjustments. Validation numbers (1-minute GC bars, 2024-10-21 ->
2026-10-20, $500 account / $150 risk / <= 5 contracts): 224 trades,
+$2,191.00 / +26.26R, trough $355.00, both halves positive.

How a day runs:
  1. Resolve the MGC front-month contract (contract/suggest via the
     existing tradovate/ market-data adapter).
  2. Feed closed 1-minute bars (09:30-16:00 ET) into the same
     FCREngine the backtester uses, configured by config_gold.yaml.
  3. The moment the engine confirms an entry (its simulated position
     opens), place the identical trade on the demo account as a
     market entry with the engine's stop/target attached as an OSO
     bracket (order/placeOSO via the existing execution adapter).
     The bracket then manages the stop/target on the broker side.
  4. At the 15:55 ET bar, liquidate whatever is still open
     (order/liquidatePosition via flatten_all) — the engine flattens
     its simulation on the same bar. One trade per day, enforced by
     the engine.
  5. If the bar stream ends mid-trade, force-close the simulation
     and flatten the demo position as well.

SAFETY — hard-enforced, not configurable:
  * The runner refuses to start unless LIVE_TRADING_ENABLED is False
    in tradovate.config, and refuses an execution client whose base
    URL is not the demo host. The tradovate package itself refuses
    every non-demo environment while the kill switch is off.
  * The strategy config is validated against the frozen validated
    spec (MGC, $10/pt, $150 risk, <= 5 contracts, 3.0 RR, and NO
    variant flags — partials / min-FVG / trend / fc-filter all off).
    Any drift raises instead of trading a different strategy.
  * Credentials come only from TRADOVATE_DEMO_* environment
    variables via TradovateConfig.from_env(). Nothing is read from
    files in this repo and nothing is printed.

STATUS: built 2026-10-21 and covered by mocked unit tests
(tests/test_gold_bot.py). It is NOT deployed anywhere, has never
connected to the real Tradovate API, and has never placed an order —
no Tradovate account or demo API key exists yet. A live (real-money)
version is out of scope by construction; see the README "Gold bot"
section for what Matt still needs to do before a first demo run.

Bars can come from the market-data WebSocket
(MarketDataAdapter.stream_bars — the in-progress bar is re-emitted as
it builds, so bars are deduplicated to each minute's final update) or
from any iterable of closed bars: run_day() accepts a plain list,
which is how the tests drive it and how --bars-csv replays a recorded
day.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional
from zoneinfo import ZoneInfo

from fcr.backtest import SESSION_END, SESSION_START
from fcr.config import load_config
from fcr.data import load_csv
from fcr.engine import FCREngine
from fcr.patterns import Bar
from tradovate.client import TradovateAPIError, TradovateClient
from tradovate.config import (
    DEMO_BASE_URL,
    LIVE_TRADING_ENABLED,
    LiveTradingDisabledError,
    MissingCredentialsError,
    TradovateConfig,
)
from tradovate.execution import ExecutionAdapter, build_bracket_entry_payload
from tradovate.market_data import MarketDataAdapter

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
GOLD_ROOT = "MGC"

# The frozen validated spec (results/gold24.json, run
# mgc_1m_24mo_v0_baseline). The runner trades THIS, not whatever a
# config file happens to say — drift is a hard error.
FROZEN_SPEC = {
    "symbol": "MGC",
    "point_value_usd": 10.0,
    "risk_budget_usd": 150.0,
    "max_units": 5,
    "rr_target": 3.0,
}
# Variant flags that must all be OFF for the validated V0 baseline.
VARIANT_FLAGS = ("partials", "trend_filter", "min_fvg_atr_mult", "fc_range_limit")


def validate_strategy_config(cfg: dict) -> dict:
    """Return cfg if it is exactly the frozen gold V0 spec; else raise.

    This is what "the winning variant is frozen in" means mechanically:
    the runner cannot be pointed at a retuned config by accident.
    """
    problems = []
    if cfg.get("symbol") != FROZEN_SPEC["symbol"]:
        problems.append(f"symbol must be 'MGC', got {cfg.get('symbol')!r}")
    for key in ("point_value_usd", "risk_budget_usd", "rr_target"):
        if float(cfg.get(key, -1)) != FROZEN_SPEC[key]:
            problems.append(f"{key} must be {FROZEN_SPEC[key]}, got {cfg.get(key)!r}")
    # load_config leaves both max_contracts (raw) and max_units
    # (normalized) in the dict — the frozen cap must hold for each.
    for cap_key in ("max_units", "max_contracts"):
        if cap_key in cfg and cfg[cap_key] is not None \
                and int(cfg[cap_key]) != FROZEN_SPEC["max_units"]:
            problems.append(f"max contracts must be 5, got {cfg[cap_key]!r}")
    if "max_units" not in cfg and "max_contracts" not in cfg:
        problems.append("max contracts must be 5, got no cap at all")
    for flag in VARIANT_FLAGS:
        if cfg.get(flag):
            problems.append(
                f"variant flag {flag!r} is set — the validated winner is "
                "the V0 baseline with every adjustment OFF"
            )
    if problems:
        raise ValueError(
            "Refusing to run: strategy config is not the frozen validated "
            "gold spec (results/gold24.json, V0 baseline). " + "; ".join(problems)
        )
    return cfg


def dedupe_closed_bars(bars: Iterable[Bar]) -> Iterator[Bar]:
    """Collapse a realtime bar stream to closed bars.

    The md/getChart stream re-emits the in-progress bar with the same
    timestamp as it builds (see tradovate.market_data). The engine —
    like the backtester — expects each minute once, final values only:
    emit a bar when the timestamp advances, keeping the last update
    per timestamp; the final buffered bar is emitted at stream end.
    """
    pending: Optional[Bar] = None
    for bar in bars:
        if pending is not None and bar.ts != pending.ts:
            yield pending
        pending = bar
    if pending is not None:
        yield pending


@dataclass
class DayResult:
    date: str
    contract_name: str
    contract_id: int
    account_id: int
    entered: bool
    orders: list = field(default_factory=list)          # bracket entries placed
    flattened: bool = False
    flatten_results: list = field(default_factory=list)
    trades: list = field(default_factory=list)          # engine trade rows


class GoldBotRunner:
    """One MGC FCR day: engine in lockstep with a demo bracket order."""

    def __init__(
        self,
        strategy_config: dict,
        market_data: MarketDataAdapter,
        execution,
        account_id: Optional[int] = None,
        contract_override: Optional[dict] = None,
    ):
        # Kill switch first: this build was reviewed for the OFF
        # position only. If the constant is ever flipped, this runner
        # still refuses — a live runner is a separate reviewed build.
        if LIVE_TRADING_ENABLED:
            raise LiveTradingDisabledError(
                "Refusing to start the gold bot: LIVE_TRADING_ENABLED is "
                "not False. This runner is a DEMO-only build; live "
                "trading needs a separate, reviewed implementation."
            )
        base_url = getattr(getattr(execution, "client", None), "config", None)
        base_url = getattr(base_url, "base_url", None)
        if base_url is not None and base_url != DEMO_BASE_URL:
            raise LiveTradingDisabledError(
                f"Refusing to start the gold bot: execution client base "
                f"URL is {base_url!r}, not the demo host {DEMO_BASE_URL!r}."
            )
        self.cfg = validate_strategy_config(dict(strategy_config))
        self.market_data = market_data
        self.execution = execution
        self.account_id = account_id
        self.contract_override = contract_override
        self.engine: Optional[FCREngine] = None
        self.date: Optional[str] = None
        self.contract_name: Optional[str] = None
        self.contract_id: Optional[int] = None
        self._entered = False
        self._orders: list = []
        self._flattened = False
        self._flatten_results: list = []
        self._last_bar: Optional[Bar] = None

    # ------------------------------------------------------------ day flow
    def start_day(self, date_iso: str, today: Optional[date] = None) -> None:
        if self.contract_override is not None:
            contract = self.contract_override
        else:
            contract = self.market_data.resolve_front_month(GOLD_ROOT, today=today)
        if contract.get("id") is None or not contract.get("name"):
            raise TradovateAPIError(
                f"Front-month resolution for {GOLD_ROOT} returned an "
                f"incomplete contract: {contract!r}"
            )
        self.contract_name = str(contract["name"])
        self.contract_id = int(contract["id"])
        if self.account_id is None:
            accounts = self.execution.get_accounts()
            if not accounts:
                raise TradovateAPIError(
                    "account/list returned no accounts for this demo login."
                )
            self.account_id = int(accounts[0]["id"])
        self.date = date_iso
        self.engine = FCREngine(self.cfg)
        self.engine.start_day(date_iso)
        self._entered = False
        self._orders = []
        self._flattened = False
        self._flatten_results = []
        self._last_bar = None

    def on_closed_bar(self, bar: Bar) -> None:
        """Feed one closed 1-minute bar (the run_day / stream core)."""
        if self.engine is None or self.date is None:
            raise RuntimeError("start_day() must be called before feeding bars")
        if bar.ts.date().isoformat() != self.date:
            return
        t = bar.ts.time()
        if not (SESSION_START <= t < SESSION_END):
            return  # outside the RTH session the backtester uses: ignore
        self._last_bar = bar
        was_open = self.engine.position is not None
        self.engine.on_bar(bar)
        if not was_open and self.engine.position is not None and not self._entered:
            self._place_entry()
        if t >= self.cfg["flatten_time"] and not self._flattened:
            self._flatten()

    def finish_day(self) -> DayResult:
        if self.engine is None:
            raise RuntimeError("start_day() must be called before finish_day()")
        if self.engine.position is not None and self._last_bar is not None:
            # Stream/data ended mid-trade: close the simulation at the
            # last bar and flatten the demo position to match.
            self.engine.force_close(
                self._last_bar.ts, self._last_bar.close, reason="DATA_END"
            )
            if not self._flattened:
                self._flatten()
        return DayResult(
            date=self.date or "",
            contract_name=self.contract_name or "",
            contract_id=self.contract_id or 0,
            account_id=self.account_id or 0,
            entered=self._entered,
            orders=list(self._orders),
            flattened=self._flattened,
            flatten_results=list(self._flatten_results),
            trades=[t.as_row() for t in self.engine.trades],
        )

    def run_day(
        self,
        date_iso: str,
        bars: Iterable[Bar],
        today: Optional[date] = None,
    ) -> DayResult:
        """Run one day over an iterable of closed bars (tests, replays)."""
        self.start_day(date_iso, today=today)
        for bar in bars:
            self.on_closed_bar(bar)
        return self.finish_day()

    def run_stream_day(
        self,
        date_iso: str,
        socket,
        today: Optional[date] = None,
        max_messages: Optional[int] = None,
    ) -> DayResult:
        """Run one day off the market-data WebSocket (injectable socket)."""
        self.start_day(date_iso, today=today)
        md_token = self.market_data.client.get_md_access_token()
        raw = self.market_data.stream_bars(
            socket,
            self.contract_name,
            md_token,
            max_messages=max_messages,
        )
        for bar in dedupe_closed_bars(raw):
            self.on_closed_bar(bar)
        return self.finish_day()

    # -------------------------------------------------------------- orders
    def _place_entry(self) -> None:
        pos = self.engine.position
        response = self.execution.place_bracket_entry(
            account_id=self.account_id,
            contract_id=self.contract_id,
            direction=pos.direction,
            contracts=int(pos.units),
            stop_price=float(pos.stop),
            target_price=float(pos.target),
            symbol=self.contract_name,
        )
        self._orders.append({
            "kind": "bracket_entry",
            "direction": pos.direction,
            "contracts": int(pos.units),
            "entry_price": float(pos.entry_price),
            "stop": float(pos.stop),
            "target": float(pos.target),
            "response": response,
        })
        self._entered = True

    def _flatten(self) -> None:
        self._flatten_results = list(
            self.execution.flatten_all(self.account_id, self.contract_id)
        )
        self._flattened = True


class RecordingExecution:
    """Dry-run execution stand-in: records, never sends.

    Implements the ExecutionAdapter surface the runner uses. Entry
    payloads are built with the real build_bracket_entry_payload so
    geometry validation is identical; nothing reaches a transport.
    """

    def __init__(self, account_id: int = 0):
        self._account_id = account_id
        self.entries: list = []
        self.flatten_calls: list = []

    def get_accounts(self) -> list:
        return [{"id": self._account_id, "name": "DRY-RUN"}]

    def place_bracket_entry(self, account_id, contract_id, direction,
                            contracts, stop_price, target_price, symbol=None):
        payload = build_bracket_entry_payload(
            account_id=account_id,
            contract_id=contract_id,
            direction=direction,
            contracts=contracts,
            stop_price=stop_price,
            target_price=target_price,
            account_spec="DRY-RUN",
            symbol=symbol,
        )
        self.entries.append(payload)
        return {"orderId": -1, "dryRun": True}

    def flatten_all(self, account_id, contract_id) -> list:
        self.flatten_calls.append((account_id, contract_id))
        return []


def _default_socket_factory(url: str):
    """Thin websocket-client wrapper (lazy import; only for real runs)."""
    try:
        import websocket  # websocket-client, not a project dependency
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Live streaming needs the 'websocket-client' package "
            "(pip install websocket-client). The runner core and the "
            "--bars-csv replay mode do not need it."
        ) from exc
    ws = websocket.create_connection(url, timeout=30)  # pragma: no cover

    class _Socket:  # pragma: no cover - thin shim over the real socket
        def send(self, text: str) -> None:
            ws.send(text)

        def recv(self) -> str:
            return ws.recv()

    return _Socket()


def build_runner(strategy_cfg: dict, dry_run: bool = False) -> GoldBotRunner:
    """Assemble the runner from TRADOVATE_DEMO_* env credentials.

    With dry_run=True no credentials are read and nothing can be
    sent: a RecordingExecution and a fixed contract stand in."""
    if dry_run:
        return GoldBotRunner(
            strategy_cfg,
            market_data=None,  # type: ignore[arg-type]
            execution=RecordingExecution(),
            contract_override={"id": 0, "name": "MGC-DRYRUN"},
        )
    tv_config = TradovateConfig.from_env()
    client = TradovateClient(tv_config)
    market_data = MarketDataAdapter(client)
    execution = ExecutionAdapter(client, account_spec=tv_config.username)
    return GoldBotRunner(strategy_cfg, market_data, execution)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="FCR gold bot — one MGC day on the Tradovate DEMO API (paper only)"
    )
    parser.add_argument("--config", default=str(ROOT / "config_gold.yaml"))
    parser.add_argument("--date", default=datetime.now(ET).date().isoformat(),
                        help="Trading day to run, YYYY-MM-DD (ET). Default: today.")
    parser.add_argument("--bars-csv", help="Replay the day from a 1-minute OHLCV CSV instead of streaming")
    parser.add_argument("--dry-run", action="store_true",
                        help="Record intended orders; never send anything, no credentials needed")
    args = parser.parse_args(argv)

    strategy_cfg = load_config(args.config)
    try:
        runner = build_runner(strategy_cfg, dry_run=args.dry_run)
    except MissingCredentialsError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.bars_csv:
        df = load_csv(args.bars_csv)
        day = df[df["timestamp"].dt.date.astype(str) == args.date]
        bars = [
            Bar(ts=ts.to_pydatetime(), open=float(o), high=float(h),
                low=float(l), close=float(c), volume=float(v))
            for ts, o, h, l, c, v in zip(
                day["timestamp"], day["open"], day["high"],
                day["low"], day["close"], day["volume"],
            )
        ]
        result = runner.run_day(args.date, bars)
    else:
        if args.dry_run:
            print("--dry-run needs --bars-csv (no credentials, no stream).",
                  file=sys.stderr)
            return 2
        socket = _default_socket_factory(
            runner.market_data.client.config.market_data_ws_url
        )
        result = runner.run_stream_day(args.date, socket)

    print(f"Gold bot day {result.date}: contract {result.contract_name} "
          f"(id {result.contract_id}), account {result.account_id}")
    if result.entered:
        for order in result.orders:
            print(f"  bracket entry placed: {order['direction']} "
                  f"{order['contracts']} @ market, stop {order['stop']}, "
                  f"target {order['target']} -> {order['response']}")
    else:
        print("  no confirmed setup — no order placed")
    for trade in result.trades:
        print(f"  engine trade: {trade['direction']} {trade['contracts']} "
              f"{trade['entry_time']} @ {trade['entry_price']} -> "
              f"{trade['exit_time']} @ {trade['exit_price']} "
              f"({trade['exit_reason']}), ${trade['pnl_usd']:+.2f} "
              f"({trade['r_multiple']:+.2f}R)")
    print(f"  flattened at/after 15:55 ET: {result.flattened} "
          f"({len(result.flatten_results)} liquidation(s) sent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
