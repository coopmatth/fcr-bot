"""Contract selection for the ORB-15 options variant — pure logic.

The rules are frozen in ORB15_OPTIONS_SPEC.md (rules 5–7): long
signal -> call, short signal -> put; expiry = the NEAREST listed
SPY expiry on/after today (0DTE when listed); strike = the listed
strike closest to the signal price, an exact tie resolving toward
in-the-money (call -> lower strike, put -> higher strike). No
per-trade discretion exists anywhere in this module: the same
inputs always select the same contract.

Contracts arrive as Alpaca option-contract objects
(``GET /v2/options/contracts`` on the trading host — see
alpaca/options_broker.py) with fields:

* ``symbol``           OCC symbol, e.g. "SPY251021C00580000"
* ``underlying_symbol``  "SPY"
* ``type``             "call" | "put"
* ``expiration_date``  "YYYY-MM-DD"
* ``strike_price``     string dollars, e.g. "580.00"
* ``tradable``         bool

OCC symbols are also parsed directly (:func:`parse_occ_symbol`) —
reconciliation uses that to recognize SPY option positions in the
account without trusting any recorded state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as date_type
from typing import Optional, Sequence

OPTION_TYPE_FOR_DIRECTION = {"long": "call", "short": "put"}

# OCC: root letters + YYMMDD + C/P + 8-digit strike (5 whole + 3
# decimal digits, i.e. strike = digits / 1000).
_OCC_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass(frozen=True)
class OptionContract:
    symbol: str            # OCC symbol
    underlying: str        # e.g. "SPY"
    option_type: str       # "call" | "put"
    expiry: date_type
    strike: float
    tradable: bool = True

    @property
    def occ_type_letter(self) -> str:
        return "C" if self.option_type == "call" else "P"


def parse_occ_symbol(symbol: str) -> Optional[OptionContract]:
    """Parse an OCC option symbol into an OptionContract, or None
    when the symbol is not an OCC option symbol at all (e.g. an
    equity symbol like "SPY"). The 2-digit OCC year maps to 20YY,
    matching every listed SPY contract this runner will ever see."""
    match = _OCC_RE.match(str(symbol).strip().upper())
    if match is None:
        return None
    root, yy, mm, dd, letter, strike_digits = match.groups()
    try:
        expiry = date_type(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return OptionContract(
        symbol=str(symbol).strip().upper(),
        underlying=root,
        option_type="call" if letter == "C" else "put",
        expiry=expiry,
        strike=int(strike_digits) / 1000.0,
    )


def is_option_symbol_for(symbol: str, underlying: str) -> bool:
    """True when ``symbol`` is an OCC option on ``underlying``."""
    parsed = parse_occ_symbol(symbol)
    return parsed is not None and parsed.underlying == underlying.upper()


def contract_from_api(raw: dict) -> Optional[OptionContract]:
    """An Alpaca /v2/options/contracts object -> OptionContract.

    Returns None for anything malformed (missing symbol/type/date/
    strike) — a contract we cannot fully understand is never a
    selection candidate."""
    try:
        symbol = str(raw["symbol"])
        option_type = str(raw["type"]).lower()
        if option_type not in ("call", "put"):
            return None
        expiry = date_type.fromisoformat(str(raw["expiration_date"])[:10])
        strike = float(raw["strike_price"])
        underlying = str(raw.get("underlying_symbol") or "")
        if not underlying:
            parsed = parse_occ_symbol(symbol)
            underlying = parsed.underlying if parsed else ""
        if not underlying:
            return None
        return OptionContract(
            symbol=symbol,
            underlying=underlying.upper(),
            option_type=option_type,
            expiry=expiry,
            strike=strike,
            tradable=bool(raw.get("tradable", True)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def select_contract(
    contracts: Sequence[OptionContract],
    direction: str,
    signal_price: float,
    today: date_type,
) -> Optional[OptionContract]:
    """The frozen selection rule (spec rules 5–7).

    Candidates: the direction's type, tradable, expiry >= today.
    Winner: the nearest expiry; within it, the strike closest to
    ``signal_price`` (ties toward in-the-money). None when no
    candidate exists — the caller logs and skips the day."""
    if direction not in OPTION_TYPE_FOR_DIRECTION:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    want_type = OPTION_TYPE_FOR_DIRECTION[direction]
    candidates = [
        c
        for c in contracts
        if c.option_type == want_type and c.tradable and c.expiry >= today
    ]
    if not candidates:
        return None
    nearest_expiry = min(c.expiry for c in candidates)
    same_expiry = [c for c in candidates if c.expiry == nearest_expiry]

    def sort_key(c: OptionContract):
        distance = abs(c.strike - signal_price)
        # Tie-break toward in-the-money: a call prefers the LOWER
        # strike, a put the HIGHER strike.
        tiebreak = c.strike if want_type == "call" else -c.strike
        return (distance, tiebreak)

    return min(same_expiry, key=sort_key)
