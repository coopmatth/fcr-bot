"""Tradovate execution adapter — DEMO ONLY, guarded by the kill switch.

The FCR strategy's order, translated: at a bar close, enter at market
with a bracket — a stop-loss and a take-profit at fixed prices,
attached to the entry so they go live on fill and cancel each other
(OCO). Tradovate's documented way to send that in one call is
``order/placeOSO``: the entry order carries ``bracket1`` /
``bracket2``, which are *sent* when the entry fills (OSO), and the
two brackets then act as an OCO pair.

Verified against the official documentation / Tradovate sources:

* ``placeOSO`` payload shape — staff example on the official
  Tradovate community forum: entry fields ``accountSpec``,
  ``accountId``, ``action`` ("Buy"/"Sell"), ``symbol``, ``orderQty``,
  ``orderType``, ``price`` / ``stopPrice``, ``isAutomated``, plus
  ``bracket1`` / ``bracket2`` each with their own ``action`` and
  ``orderType`` ("Limit" needs ``price``; "Stop" needs
  ``stopPrice``); a filled example returns ``{orderId, oso1Id,
  oso2Id}``:
  https://community.tradovate.com/t/i-can-send-oso-order-in-demo-but-access-denied-in-replay/4459
  and https://community.tradovate.com/t/orderstrategy-with-a-mix-of-oco-and-oso/3874
* Plain order placement is ``order/placeorder`` and positions are
  read with ``position/list``:
  https://docs.ninjatrader.com/api/getting-started
* Endpoint inventory from the official OpenAPI spec — ``order/list``,
  ``order/placeOrder``, ``order/liquidatePosition``,
  ``position/list``, ``account/list``:
  https://deepwiki.com/tradovate/example-api-csharp-trading/3.1-api-specification
* Liquidation body: the spec lists the operation but the doc pages
  reachable without an account do not publish its exact request
  schema. This adapter sends the position's identifying entity
  fields — ``accountId`` + ``contractId`` (the field names every
  Position/Order entity uses, e.g. in the WebSocket docs' entity
  example at https://docs.ninjatrader.com/api/websockets) — plus
  ``isAutomated``. CONFIRM against the API Reference playground with
  Matt's API key before first real (demo) use; see the project
  report. ``flatten_all`` only ever liquidates positions that
  ``position/list`` actually returned for that account+contract.

HARD SAFETY: the constructor (and every public method, via
``_assert_demo``) refuses any environment other than demo while
``LIVE_TRADING_ENABLED`` is False, and refuses if the underlying
client's base URL is not the demo host. There is no code path in
this module that can place an order against live.
"""
from __future__ import annotations

from typing import List, Optional

from .client import TradovateClient
from .config import (
    DEMO_BASE_URL,
    LIVE_TRADING_ENABLED,
    LiveTradingDisabledError,
    assert_demo_environment,
)

__all__ = [
    "ExecutionAdapter",
    "LIVE_TRADING_ENABLED",
    "LiveTradingDisabledError",
    "build_bracket_entry_payload",
]

# direction (engine vocabulary) -> (entry action, exit action)
_DIRECTIONS = {
    "long": ("Buy", "Sell"),
    "buy": ("Buy", "Sell"),
    "short": ("Sell", "Buy"),
    "sell": ("Sell", "Buy"),
}


def build_bracket_entry_payload(
    account_id: int,
    contract_id,
    direction: str,
    contracts: int,
    stop_price: float,
    target_price: float,
    account_spec: str = "",
    symbol: Optional[str] = None,
) -> dict:
    """Build the documented ``order/placeOSO`` bracket-entry payload.

    ``contract_id`` is the resolved MNQ contract: pass the contract
    *symbol* string (e.g. ``"MNQZ6"`` — the identifier the OSO
    examples use) or an integer contract id; the adapter puts each
    under its own field name (``symbol`` / ``contractId``) and never
    invents the other. ``symbol=`` overrides a string contract_id.

    Geometry is validated: for a long, stop < target; for a short,
    stop > target — a crossed bracket is a build error, not an order.
    """
    key = str(direction).strip().lower()
    if key not in _DIRECTIONS:
        raise ValueError(
            f"direction must be one of {sorted(_DIRECTIONS)}; got {direction!r}"
        )
    entry_action, exit_action = _DIRECTIONS[key]
    if int(contracts) <= 0:
        raise ValueError(f"contracts must be positive; got {contracts!r}")
    if entry_action == "Buy" and not stop_price < target_price:
        raise ValueError(
            f"long bracket needs stop_price < target_price; "
            f"got stop={stop_price}, target={target_price}"
        )
    if entry_action == "Sell" and not stop_price > target_price:
        raise ValueError(
            f"short bracket needs stop_price > target_price; "
            f"got stop={stop_price}, target={target_price}"
        )
    payload = {
        "accountSpec": account_spec,
        "accountId": int(account_id),
        "action": entry_action,
        "orderQty": int(contracts),
        "orderType": "Market",
        "isAutomated": True,
        # Take-profit leg: a Limit at the fixed target price.
        "bracket1": {
            "action": exit_action,
            "orderType": "Limit",
            "price": float(target_price),
        },
        # Stop-loss leg: a Stop at the fixed stop price.
        "bracket2": {
            "action": exit_action,
            "orderType": "Stop",
            "stopPrice": float(stop_price),
        },
    }
    resolved_symbol = symbol if symbol is not None else (
        contract_id if isinstance(contract_id, str) else None
    )
    if resolved_symbol is not None:
        payload["symbol"] = resolved_symbol
    else:
        payload["contractId"] = int(contract_id)
    return payload


class ExecutionAdapter:
    """Places/reads/flattens orders on the DEMO environment only."""

    def __init__(
        self,
        client: TradovateClient,
        environment: str = "demo",
        account_spec: str = "",
    ):
        # Kill-switch guard FIRST, before anything is stored or sent:
        # any non-demo environment raises while the switch is off.
        assert_demo_environment(environment)
        if client.config.base_url != DEMO_BASE_URL:
            raise LiveTradingDisabledError(
                "Refusing ExecutionAdapter: the client's base URL is "
                f"{client.config.base_url!r}, not the demo base URL "
                f"{DEMO_BASE_URL!r}. Execution is demo-only while "
                "LIVE_TRADING_ENABLED is False."
            )
        self.client = client
        self.environment = environment
        self.account_spec = account_spec

    # -------------------------------------------------------------- safety
    def _assert_demo(self) -> None:
        """Re-checked on every public method (defense in depth)."""
        assert_demo_environment(self.environment)
        if self.client.config.base_url != DEMO_BASE_URL:
            raise LiveTradingDisabledError(
                "Refusing to trade: client base URL is not the demo host."
            )

    # ---------------------------------------------------------------- reads
    def get_accounts(self) -> list:
        """GET /account/list — the caller's accounts (ids are ints)."""
        self._assert_demo()
        result = self.client.get("/account/list")
        return list(result or [])

    def get_positions(self) -> list:
        """GET /position/list — open positions for the session."""
        self._assert_demo()
        result = self.client.get("/position/list")
        return list(result or [])

    def get_orders(self) -> list:
        """GET /order/list — orders for the session."""
        self._assert_demo()
        result = self.client.get("/order/list")
        return list(result or [])

    # --------------------------------------------------------------- orders
    def place_bracket_entry(
        self,
        account_id: int,
        contract_id,
        direction: str,
        contracts: int,
        stop_price: float,
        target_price: float,
        symbol: Optional[str] = None,
    ) -> dict:
        """Market entry + attached stop/target bracket via placeOSO.

        Returns the documented response shape ``{orderId, oso1Id,
        oso2Id}`` on success; API failures raise TradovateAPIError
        (including failureText bodies) from the client layer."""
        self._assert_demo()
        payload = build_bracket_entry_payload(
            account_id=account_id,
            contract_id=contract_id,
            direction=direction,
            contracts=contracts,
            stop_price=stop_price,
            target_price=target_price,
            account_spec=self.account_spec,
            symbol=symbol,
        )
        return self.client.post("/order/placeOSO", body=payload)

    def flatten_all(self, account_id: int, contract_id) -> List[dict]:
        """Liquidate the open position in one contract on one account.

        Reads ``position/list`` first and calls the documented
        ``order/liquidatePosition`` once per matching open position
        (``netPos != 0``). With no matching open position it sends
        nothing and returns []. ``contract_id`` may be the integer
        contract id (positions carry ``contractId`` ints) or the
        symbol string if it was resolved via find_contract — integer
        ids are what positions report, so int is the reliable form."""
        self._assert_demo()
        results: List[dict] = []
        for position in self.get_positions():
            if int(position.get("accountId", -1)) != int(account_id):
                continue
            if isinstance(contract_id, int) or (
                isinstance(contract_id, str) and contract_id.isdigit()
            ):
                if int(position.get("contractId", -1)) != int(contract_id):
                    continue
            # A string symbol cannot be matched against position rows
            # (they carry ids, not symbols) — the caller must resolve
            # it with MarketDataAdapter.find_contract first.
            elif contract_id is not None:
                raise ValueError(
                    "flatten_all needs the integer contractId that "
                    "positions report; resolve a symbol via "
                    "MarketDataAdapter.find_contract(symbol)['id'] first."
                )
            if not position.get("netPos"):
                continue
            body = {
                "accountId": int(account_id),
                "contractId": int(position["contractId"]),
                "isAutomated": True,
            }
            results.append(self.client.post("/order/liquidatePosition", body=body))
        return results
