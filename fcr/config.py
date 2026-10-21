"""Config loading: YAML file -> dict with time fields parsed to datetime.time.

Also normalizes the sizing keys so the engine has one contract regardless
of which instrument the YAML describes:

  risk_budget_usd   dollars risked per trade (falls back to the legacy
                    risk_per_trade_usd key)
  point_value_usd   $ per 1.00 price point per unit (1.0 for equities,
                    2.0 for MNQ futures)
  max_units         hard cap on units per trade (max_contracts or
                    max_shares), or None when uncapped
  instrument_type   'futures' or 'equity'
  starting_balance_usd  paper account starting equity, or None
"""
from __future__ import annotations

from datetime import time
from pathlib import Path

import yaml

_TIME_KEYS = ("first_candle_start", "first_candle_end", "entry_cutoff", "flatten_time")


def _parse_time(value) -> time:
    parts = str(value).split(":")
    return time(int(parts[0]), int(parts[1]))


def load_config(path) -> dict:
    raw = yaml.safe_load(Path(path).read_text())
    cfg = dict(raw)
    for key in _TIME_KEYS:
        cfg[key] = _parse_time(cfg[key])
    cfg["rr_target"] = float(cfg["rr_target"])

    # --- sizing normalization -------------------------------------------
    if "risk_budget_usd" in cfg:
        cfg["risk_budget_usd"] = float(cfg["risk_budget_usd"])
    elif "risk_per_trade_usd" in cfg:
        cfg["risk_budget_usd"] = float(cfg["risk_per_trade_usd"])
    else:
        raise ValueError("config needs risk_budget_usd (or legacy risk_per_trade_usd)")
    cfg["risk_per_trade_usd"] = cfg["risk_budget_usd"]  # legacy alias

    cfg["point_value_usd"] = float(cfg.get("point_value_usd", 1.0))

    cap = cfg.get("max_contracts", cfg.get("max_shares"))
    cfg["max_units"] = int(cap) if cap is not None else None

    cfg.setdefault("instrument_type", "futures" if cfg["max_units"] is not None else "equity")
    if "starting_balance_usd" in cfg and cfg["starting_balance_usd"] is not None:
        cfg["starting_balance_usd"] = float(cfg["starting_balance_usd"])
    else:
        cfg["starting_balance_usd"] = None
    return cfg
