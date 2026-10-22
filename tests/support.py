"""Helpers for building mock HTTP responses in tests."""

import json
from typing import Any

import httpx


def json_response(payload: Any) -> httpx.Response:
    """A 200 JSON response. Decimals are written as strings, which the schemas accept."""
    return httpx.Response(200, text=json.dumps(payload, default=str), headers={"content-type": "application/json"})


def route(responses: dict[str, Any]):
    """Mock handler that serves one payload per URL path. Payloads may be callables taking the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = responses.get(request.url.path)
        if payload is None:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        return json_response(payload(request) if callable(payload) else payload)

    return handler
