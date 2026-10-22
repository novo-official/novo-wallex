"""Read-only HTTP client for the Wallex REST API.

Phase 0 exposes GET requests only. Order placement and cancellation are added
in the execution phase, after the official docs have been confirmed.

Responses are returned as parsed JSON (`Any`). Typed models are added after
`scripts/probe_api.py` has confirmed the real response shapes.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from .ratelimit import RateLimiter

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class WallexError(Exception):
    """Base class for all client errors."""


class WallexAPIError(WallexError):
    """The API answered with a non-2xx status."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Wallex API returned HTTP {status_code}: {body[:300]}")


class WallexResponseError(WallexError):
    """The API answered 2xx but the body is not valid JSON."""


class WallexTransportError(WallexError):
    """No HTTP response could be obtained after all retries."""


class WallexClient:
    """GET client with timeout, rate limiting and exponential-backoff retries.

    Retries apply only to GET requests and to transient failures: connection
    errors, timeouts, 429 and 5xx. Other 4xx responses are returned immediately.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        rate_limiter: RateLimiter | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            logger.warning("WALLEX_API_KEY is empty: only public endpoints will work")
        headers = {"x-api-key": api_key} if api_key else {}
        self._http = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._rate_limiter = rate_limiter
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WallexClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Send a GET and return the final response, even when its status is not 2xx.

        Raises WallexTransportError only when no response was received at all.
        """
        reason = "no attempt made"
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                delay = self._backoff_seconds * 2 ** (attempt - 1)
                logger.warning("GET %s failed (%s); retry %d in %.1fs", path, reason, attempt, delay)
                self._sleep(delay)
            if self._rate_limiter is not None:
                self._rate_limiter.wait()
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                response = None
                reason = f"transport error: {exc}"
                continue
            if response.status_code not in RETRYABLE_STATUS_CODES:
                return response
            reason = f"HTTP {response.status_code}"
        if response is None:
            raise WallexTransportError(f"GET {path} failed after {self._max_retries + 1} attempts ({reason})")
        return response

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET and parse JSON. Raises WallexAPIError for non-2xx responses."""
        response = self.get(path, params)
        if not response.is_success:
            raise WallexAPIError(response.status_code, response.text)
        try:
            # parse_float=Decimal: every JSON number becomes an exact Decimal, never a float.
            return json.loads(response.text, parse_float=Decimal)
        except ValueError as exc:
            raise WallexResponseError(f"GET {path} returned non-JSON body: {response.text[:300]}") from exc

    # --- Read-only endpoints ----------------------------------------------------

    def get_markets(self) -> Any:
        return self.get_json("/hector/web/v1/markets")

    def get_depth_all(self) -> Any:
        # UNVERIFIED path: the docs were unreachable when this was written. Confirm with the probe.
        return self.get_json("/depth/all")

    def get_udf_history(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> Any:
        params = {"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts}
        return self.get_json("/v1/udf/history", params)

    def get_open_orders(self) -> Any:
        return self.get_json("/v1/account/openOrders")

    def get_orders(self) -> Any:
        return self.get_json("/v1/account/orders")

    def get_order(self, client_id: str) -> Any:
        return self.get_json(f"/v1/account/orders/{quote(client_id, safe='')}")

    def get_prop_package_history(self) -> Any:
        return self.get_json("/v1/prop/user/packages/history")
