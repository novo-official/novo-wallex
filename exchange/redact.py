"""Redaction of account identifiers and balances before anything is printed, logged or committed.

The repository is public. Probe output and fixtures from private endpoints
(`/v1/account/...`, `/v1/prop/...`) can contain order ids, user ids and
balances, so they are redacted by default. Public market data is left as is.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"

PRIVATE_PATH_PREFIXES = ("/v1/account", "/v1/prop")

# Keys that identify a person, an account or an order. Redacted in every response.
IDENTIFIER_KEYS = frozenset(
    {
        "client_id",
        "clientorderid",
        "user_id",
        "userid",
        "uid",
        "uuid",
        "account_id",
        "accountid",
        "email",
        "mobile",
        "phone",
        "national_code",
        "iban",
        "card_number",
        "wallet_address",
        "address",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
    }
)

# Keys that carry account amounts or ids. Redacted only in responses from private endpoints,
# because public market data uses some of the same names (e.g. "id", "value").
PRIVATE_KEYS = frozenset(
    {
        "id",
        "order_id",
        "orderid",
        "trade_id",
        "package_id",
        "balance",
        "balances",
        "start_balance",
        "final_balance",
        "warning_limit",
        "liquidity_limit",
        "available",
        "locked",
        "free",
        "value",
        "total",
        "amount",
    }
)


def _normalize(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def is_private_path(path: str) -> bool:
    return path.startswith(PRIVATE_PATH_PREFIXES)


def redact(payload: Any, *, private: bool) -> Any:
    """Return a copy of `payload` with identifier keys (and amount keys if `private`) replaced."""
    if isinstance(payload, dict):
        result = {}
        for key, value in payload.items():
            name = _normalize(str(key))
            if name in IDENTIFIER_KEYS or (private and name in PRIVATE_KEYS):
                result[key] = REDACTED if value not in (None, "", [], {}) else value
            else:
                result[key] = redact(value, private=private)
        return result
    if isinstance(payload, list):
        return [redact(item, private=private) for item in payload]
    return payload


def scrub_secret(text: str, secret: str) -> str:
    """Remove a known secret (e.g. the API key) from free text. Empty secrets are ignored."""
    if not secret:
        return text
    return re.sub(re.escape(secret), REDACTED, text)
