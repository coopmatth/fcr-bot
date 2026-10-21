"""Tradovate DEMO-only configuration for the FCR paper bot.

PAPER / DEMO ONLY. This module is the single place that knows the
Tradovate hosts, and it refuses to select the live host while the
kill switch (``LIVE_TRADING_ENABLED``) is off — which it is, in code,
on purpose. There is no setting, environment variable, or constructor
argument that turns live trading on; flipping the constant in source
*and* rebuilding is the only way, and that is a deliberate code-review
event, not a runtime option.

Verified against the official documentation (the developer site
formerly at developer.tradovate.com now lives under NinjaTrader,
which owns Tradovate):

* Hosts split by service — ``demo.tradovateapi.com`` (Demo /
  simulation), ``live.tradovateapi.com`` (Live) and
  ``md.tradovateapi.com`` (market data feed):
  https://docs.ninjatrader.com/api/authentication
* Demo REST base used throughout the docs' examples:
  ``https://demo.tradovateapi.com/v1``:
  https://docs.ninjatrader.com/api/getting-started
* Market-data WebSocket ``wss://md.tradovateapi.com/v1/websocket``:
  official Tradovate sample code quoted at
  https://community.tradovate.com/t/get-quote-tick-data/7809/4
* Trading WebSocket is the REST host with ``wss://`` substituted:
  https://docs.ninjatrader.com/api/websockets

Credentials are read from the environment ONLY — never from a file
checked into the repo, never from chat. Required variables:

* ``TRADOVATE_DEMO_USERNAME``  -> ``name`` in the token request
* ``TRADOVATE_DEMO_PASSWORD``  -> ``password``
* ``TRADOVATE_DEMO_CID``       -> ``cid`` (integer client id issued
  with the API key)
* ``TRADOVATE_DEMO_API_SECRET``-> ``sec`` (the API key secret)
* ``TRADOVATE_DEMO_API_KEY``   -> ``appId``. The docs state ``appId``
  and ``appVersion`` are free-form identifiers shown to end users for
  the connected app (Authentication & Access page above), so the API
  key's identifier doubles as the app identifier here. NOTE: exactly
  how Matt's issued key labels its parts must be confirmed against
  his Tradovate *Application Settings > API Access* screen before
  first real use — the token request fields themselves (``name`` /
  ``password`` / ``appId`` / ``appVersion`` / ``cid`` / ``sec`` /
  ``deviceId``) are the documented ones either way.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

# ---------------------------------------------------------------------------
# HARD SAFETY SWITCH. Do not flip without a full code review and Matt's
# explicit go-ahead. While this is False:
#   * no code path in this package selects the live base URL,
#   * ExecutionAdapter refuses any environment other than "demo".
# ---------------------------------------------------------------------------
LIVE_TRADING_ENABLED = False

DEMO_ENVIRONMENT = "demo"
LIVE_ENVIRONMENT = "live"

DEMO_BASE_URL = "https://demo.tradovateapi.com/v1"
# Exists only so the refusal can name it and tests can assert it is
# never selected while LIVE_TRADING_ENABLED is False.
LIVE_BASE_URL = "https://live.tradovateapi.com/v1"

DEMO_WS_URL = "wss://demo.tradovateapi.com/v1/websocket"
LIVE_WS_URL = "wss://live.tradovateapi.com/v1/websocket"
# Market data is served from its own shared host (not demo/live split).
MARKET_DATA_WS_URL = "wss://md.tradovateapi.com/v1/websocket"

APP_VERSION = "1.0"
DEFAULT_APP_ID = "FCRPaperBot"

ENV_USERNAME = "TRADOVATE_DEMO_USERNAME"
ENV_PASSWORD = "TRADOVATE_DEMO_PASSWORD"
ENV_API_KEY = "TRADOVATE_DEMO_API_KEY"
ENV_API_SECRET = "TRADOVATE_DEMO_API_SECRET"
ENV_CID = "TRADOVATE_DEMO_CID"

REQUIRED_ENV_VARS = (
    ENV_USERNAME,
    ENV_PASSWORD,
    ENV_API_KEY,
    ENV_API_SECRET,
    ENV_CID,
)


class LiveTradingDisabledError(RuntimeError):
    """Raised on any attempt to use the live environment while the
    kill switch (LIVE_TRADING_ENABLED) is off."""


class MissingCredentialsError(ValueError):
    """Raised when required TRADOVATE_DEMO_* env vars are absent.

    The message names every missing variable so the fix is obvious
    without ever printing a credential value."""


def assert_demo_environment(environment: str) -> str:
    """Return ``"demo"`` or raise. The one guard every entry point uses.

    While LIVE_TRADING_ENABLED is False, *anything* other than the
    demo environment — "live", "replay", "production", a typo — is
    refused. There is intentionally no allow-list bypass.
    """
    env = (environment or "").strip().lower()
    if env == DEMO_ENVIRONMENT:
        return env
    if not LIVE_TRADING_ENABLED:
        raise LiveTradingDisabledError(
            f"Refusing environment {environment!r}: LIVE_TRADING_ENABLED is "
            "False, so only the Tradovate DEMO environment "
            f"({DEMO_BASE_URL}) may be used. This package is paper/demo "
            "only; live trading is not implemented."
        )
    if env != LIVE_ENVIRONMENT:  # pragma: no cover - kill switch is off
        raise ValueError(f"Unknown Tradovate environment: {environment!r}")
    return env  # pragma: no cover - unreachable while switch is off


@dataclass(frozen=True)
class TradovateConfig:
    """Demo credentials + environment, built only from env vars."""

    username: str
    password: str
    api_key: str
    api_secret: str
    cid: int
    environment: str = DEMO_ENVIRONMENT

    def __post_init__(self) -> None:
        # Frozen dataclass: validate, don't mutate. Any environment
        # other than demo raises here while the kill switch is off,
        # so a live config object cannot even be constructed.
        assert_demo_environment(self.environment)

    # ------------------------------------------------------------------ env
    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "TradovateConfig":
        """Build from TRADOVATE_DEMO_* variables (os.environ by default).

        Raises MissingCredentialsError naming every missing variable.
        Values are stripped; empty strings count as missing. No value
        is ever included in an error message.
        """
        source = os.environ if environ is None else environ
        values = {}
        missing = []
        for name in REQUIRED_ENV_VARS:
            raw = source.get(name)
            if raw is None or not str(raw).strip():
                missing.append(name)
            else:
                values[name] = str(raw).strip()
        if missing:
            raise MissingCredentialsError(
                "Missing required Tradovate demo credentials: "
                + ", ".join(missing)
                + ". Set them in your local environment / .env file "
                "(see .env.example in the fcr-bot project). Credentials "
                "come from your own Tradovate Application Settings > "
                "API Access key and must never be pasted into chat or "
                "committed to the repo."
            )
        cid_raw = values[ENV_CID]
        try:
            cid = int(cid_raw)
        except ValueError:
            raise MissingCredentialsError(
                f"{ENV_CID} must be an integer (the client id issued "
                f"with your Tradovate API key); got a non-integer value."
            ) from None
        return cls(
            username=values[ENV_USERNAME],
            password=values[ENV_PASSWORD],
            api_key=values[ENV_API_KEY],
            api_secret=values[ENV_API_SECRET],
            cid=cid,
            environment=DEMO_ENVIRONMENT,
        )

    # ----------------------------------------------------------------- urls
    @property
    def base_url(self) -> str:
        """REST base URL for the configured environment.

        Re-checks the guard at access time (defense in depth): while
        the kill switch is off this can only ever return the demo URL.
        """
        env = assert_demo_environment(self.environment)
        if env == LIVE_ENVIRONMENT:  # pragma: no cover - switch is off
            return LIVE_BASE_URL
        return DEMO_BASE_URL

    @property
    def websocket_url(self) -> str:
        """Trading WebSocket URL (demo while the kill switch is off)."""
        env = assert_demo_environment(self.environment)
        if env == LIVE_ENVIRONMENT:  # pragma: no cover - switch is off
            return LIVE_WS_URL
        return DEMO_WS_URL

    @property
    def market_data_ws_url(self) -> str:
        """Market-data WebSocket URL (shared md host, per the docs)."""
        return MARKET_DATA_WS_URL
