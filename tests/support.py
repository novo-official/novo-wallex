"""Helpers for building mock HTTP responses in tests."""

import json
from typing import Any

import httpx


def json_response(payload: Any) -> httpx.Response:
    """A 200 JSON response. Decimals are written as strings, which the schemas accept."""
    return httpx.Response(200, text=json.dumps(payload, default=str), headers={"content-type": "application/json"})
