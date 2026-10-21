"""Tradovate DEMO-only integration for the FCR paper bot.

PAPER / DEMO ONLY. ``LIVE_TRADING_ENABLED`` is False in
``tradovate.config`` and, while it is, this package cannot select
the live Tradovate host and its execution adapter refuses every
environment except demo. No real credentials exist for this
project and none may be created or used through this code.

Endpoint verification (official sources) lives in each module's
docstring:

* ``config``      — hosts / environments (docs.ninjatrader.com/api/authentication)
* ``client``      — POST /auth/accesstokenrequest, GET /auth/renewaccesstoken,
                    Bearer authorized requests (docs.ninjatrader.com/api/getting-started)
* ``market_data`` — GET /contract/find, GET /contract/suggest,
                    md/getChart over wss://md.tradovateapi.com/v1/websocket
                    (docs.ninjatrader.com/market-data)
* ``execution``   — POST /order/placeOSO brackets, GET /position/list,
                    GET /order/list, POST /order/liquidatePosition
"""
from .config import (
    DEMO_BASE_URL,
    LIVE_BASE_URL,
    LIVE_TRADING_ENABLED,
    MARKET_DATA_WS_URL,
    LiveTradingDisabledError,
    MissingCredentialsError,
    TradovateConfig,
)
from .client import TradovateAPIError, TradovateClient, TradovateRateLimitError
from .execution import ExecutionAdapter, build_bracket_entry_payload
from .market_data import MarketDataAdapter, parse_bar, bars_from_frame

__all__ = [
    "DEMO_BASE_URL",
    "LIVE_BASE_URL",
    "LIVE_TRADING_ENABLED",
    "MARKET_DATA_WS_URL",
    "LiveTradingDisabledError",
    "MissingCredentialsError",
    "TradovateConfig",
    "TradovateAPIError",
    "TradovateClient",
    "TradovateRateLimitError",
    "ExecutionAdapter",
    "build_bracket_entry_payload",
    "MarketDataAdapter",
    "parse_bar",
    "bars_from_frame",
]
