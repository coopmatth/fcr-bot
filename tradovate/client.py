"""Tradovate REST client: auth, token caching/renewal, authorized calls.

HTTP uses ONLY the Python standard library (urllib) in production, and
every request goes through an injectable ``transport`` callable so
unit tests run with a fake transport and never touch the network.

Transport contract::

    transport(method, url, headers, body_bytes)
        -> (status_code: int, headers: Mapping, body: bytes)

Verified against the official documentation:

* Token request — ``POST https://demo.tradovateapi.com/v1/auth/accesstokenrequest``
  with JSON body ``name`` / ``password`` / ``appId`` / ``appVersion``
  / ``cid`` / ``sec`` / ``deviceId``; response carries ``accessToken``,
  ``mdAccessToken``, ``expirationTime``, ``userId``, ``userStatus``…:
  https://docs.ninjatrader.com/api/getting-started and
  https://docs.ninjatrader.com/api/authentication
* Token lifetime ~90 minutes; renew (don't re-request) before expiry
  via ``renewAccessToken``: same Authentication page. The endpoint
  table in the official OpenAPI spec lists it as
  ``GET /auth/renewaccesstoken``:
  https://deepwiki.com/tradovate/example-api-csharp-trading/3.1-api-specification
* Authorized requests send ``Authorization: Bearer <accessToken>``:
  https://docs.ninjatrader.com/api/getting-started
* Rate-limit time penalties come back as a JSON object with
  ``p-ticket`` / ``p-time`` (and possibly ``p-captcha``) fields; the
  request was NOT handled: Authentication page above. This client
  surfaces that as :class:`TradovateRateLimitError` and never blindly
  retries — least of all an order.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Optional, Tuple
from urllib.parse import urlencode

from .config import APP_VERSION, TradovateConfig

# transport(method, url, headers, body) -> (status, headers, body_bytes)
Transport = Callable[
    [str, str, Mapping[str, str], Optional[bytes]],
    Tuple[int, Mapping[str, str], bytes],
]

# Renew this long before expiry (docs advise ~15 minutes).
_RENEW_MARGIN = timedelta(minutes=15)
# Within this window a cached token is treated as already expired, so
# a request never leaves with a token that dies in flight.
_EXPIRY_LEEWAY = timedelta(seconds=30)


class TradovateAPIError(Exception):
    """An error response from the Tradovate API (or a transport failure
    wrapped with context). Carries the HTTP status when there is one."""

    def __init__(self, message: str, status: Optional[int] = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class TradovateRateLimitError(TradovateAPIError):
    """The documented ``p-ticket`` / ``p-time`` time-penalty response.

    The request was not handled by the server. ``p_time`` is how many
    seconds the docs say to wait before retrying with ``p_ticket``
    attached. This client raises instead of sleeping/retrying so the
    caller decides — an order must never be resent blindly."""

    def __init__(self, message: str, p_ticket=None, p_time=None, payload=None):
        super().__init__(message, status=429, payload=payload)
        self.p_ticket = p_ticket
        self.p_time = p_time


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
) -> Tuple[int, Mapping[str, str], bytes]:
    """Default transport: one urllib request. Never used in tests."""
    req = urllib.request.Request(url=url, data=body, method=method.upper())
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:  # HTTP status >= 400 is a response
        return exc.code, dict(exc.headers.items() if exc.headers else {}), exc.read()
    except urllib.error.URLError as exc:
        raise TradovateAPIError(f"Network error calling Tradovate: {exc.reason}") from exc


def _parse_iso8601(value: str) -> datetime:
    """Parse the docs' ``expirationTime`` format (``...Z``) to an
    aware UTC datetime."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decode_json(status: int, body: bytes):
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TradovateAPIError(
            f"Tradovate returned a non-JSON response (HTTP {status}).",
            status=status,
        ) from exc


def _error_text(payload) -> Optional[str]:
    """Tradovate error bodies use ``errorText`` (auth) or
    ``failureText`` / ``failureReason`` (order commands)."""
    if isinstance(payload, dict):
        for key in ("errorText", "failureText", "failureReason", "error"):
            value = payload.get(key)
            if value:
                return str(value)
    if isinstance(payload, str) and payload:
        return payload
    return None


@dataclass
class TokenInfo:
    access_token: str
    md_access_token: Optional[str]
    expiration_time: datetime  # aware, UTC
    user_id: Optional[int] = None
    raw: Optional[dict] = None

    def is_expired(self, now: datetime) -> bool:
        return self.expiration_time <= now + _EXPIRY_LEEWAY

    def expires_within(self, delta: timedelta, now: datetime) -> bool:
        return self.expiration_time <= now + delta


class TradovateClient:
    """Demo REST client with cached, renewable access tokens.

    ``clock`` is injectable (a callable returning an aware datetime)
    so token-expiry tests don't depend on wall time.
    """

    def __init__(
        self,
        config: TradovateConfig,
        transport: Optional[Transport] = None,
        clock: Optional[Callable[[], datetime]] = None,
        device_id: Optional[str] = None,
    ):
        self.config = config
        self._transport: Transport = transport or urllib_transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._device_id = device_id
        self._token: Optional[TokenInfo] = None

    # ------------------------------------------------------------ internals
    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now

    def _raw_request(self, method, url, headers, body_obj):
        body = None
        if body_obj is not None:
            body = json.dumps(body_obj).encode("utf-8")
        status, _resp_headers, raw = self._transport(method, url, headers, body)
        payload = _decode_json(status, raw)
        if isinstance(payload, dict) and payload.get("p-ticket") is not None:
            raise TradovateRateLimitError(
                "Tradovate imposed a rate-limit time penalty "
                f"(p-time={payload.get('p-time')}); the request was not "
                "handled. Do not blindly retry — least of all an order.",
                p_ticket=payload.get("p-ticket"),
                p_time=payload.get("p-time"),
                payload=payload,
            )
        error = _error_text(payload)
        # Tradovate reports auth failures with an errorText body,
        # sometimes alongside HTTP 200 — either way it is a failure.
        if status >= 400 or error is not None:
            raise TradovateAPIError(
                f"Tradovate API error (HTTP {status}): "
                f"{error or 'request failed'}",
                status=status,
                payload=payload,
            )
        return payload

    # ----------------------------------------------------------------- auth
    def build_access_token_body(self) -> dict:
        """The documented accesstokenrequest body (see module docstring)."""
        body = {
            "name": self.config.username,
            "password": self.config.password,
            "appId": self.config.api_key or "FCRPaperBot",
            "appVersion": APP_VERSION,
            "cid": self.config.cid,
            "sec": self.config.api_secret,
        }
        if self._device_id:
            body["deviceId"] = self._device_id
        return body

    def _store_token(self, payload) -> TokenInfo:
        if not isinstance(payload, dict) or not payload.get("accessToken"):
            raise TradovateAPIError(
                "Tradovate auth response did not include an accessToken.",
                payload=payload,
            )
        expiration_raw = payload.get("expirationTime")
        if expiration_raw:
            expiration = _parse_iso8601(expiration_raw)
        else:  # docs always send it; fall back to the ~90-minute lifetime
            expiration = self._now() + timedelta(minutes=90)
        self._token = TokenInfo(
            access_token=payload["accessToken"],
            md_access_token=payload.get("mdAccessToken"),
            expiration_time=expiration,
            user_id=payload.get("userId"),
            raw=payload,
        )
        return self._token

    def request_access_token(self) -> TokenInfo:
        """POST /auth/accesstokenrequest — a fresh session. Prefer
        :meth:`get_access_token`, which caches and renews."""
        url = f"{self.config.base_url}/auth/accesstokenrequest"
        payload = self._raw_request(
            "POST",
            url,
            {"Content-Type": "application/json", "Accept": "application/json"},
            self.build_access_token_body(),
        )
        return self._store_token(payload)

    def renew_access_token(self) -> TokenInfo:
        """GET /auth/renewaccesstoken with the current Bearer token.

        Documented as the way to extend a session (~15 min before
        expiry) instead of starting a new one — the user is limited
        to two concurrent sessions."""
        if self._token is None:
            return self.request_access_token()
        url = f"{self.config.base_url}/auth/renewaccesstoken"
        payload = self._raw_request(
            "GET",
            url,
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token.access_token}",
            },
            None,
        )
        return self._store_token(payload)

    def get_access_token(self) -> str:
        """Cached token, renewed/requested when (nearly) expired."""
        now = self._now()
        if self._token is None:
            self.request_access_token()
        elif self._token.is_expired(now):
            self.renew_access_token()
        elif self._token.expires_within(_RENEW_MARGIN, now):
            self.renew_access_token()
        return self._token.access_token

    def get_md_access_token(self) -> str:
        """The separate market-data token from the same auth response."""
        self.get_access_token()
        if not self._token.md_access_token:
            raise TradovateAPIError(
                "Auth response included no mdAccessToken; market data "
                "is unavailable for this API key."
            )
        return self._token.md_access_token

    @property
    def token_info(self) -> Optional[TokenInfo]:
        return self._token

    # ------------------------------------------------------- authorized API
    def authorized_request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, object]] = None,
        body: Optional[dict] = None,
    ):
        """Call ``{demo base}{path}`` with the Bearer token attached.

        ``path`` is a documented endpoint path such as
        ``/account/list`` or ``/order/placeOSO``."""
        token = self.get_access_token()
        url = f"{self.config.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self._raw_request(method, url, headers, body)

    def get(self, path: str, params: Optional[Mapping[str, object]] = None):
        return self.authorized_request("GET", path, params=params)

    def post(self, path: str, body: Optional[dict] = None):
        return self.authorized_request("POST", path, body=body if body is not None else {})
