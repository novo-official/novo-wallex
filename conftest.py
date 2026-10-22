"""Shared test fixtures: real Wallex captures and a client factory on a mock transport."""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from exchange.client import WallexClient

FIXTURE_DIR = Path(__file__).resolve().parent / "tests" / "fixtures" / "wallex"


@pytest.fixture
def load_fixture():
    """Load a JSON fixture; numbers are parsed as Decimal, matching the production client."""

    def _load(name: str):
        return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"), parse_float=Decimal)

    return _load


@pytest.fixture
def mock_client():
    """Build a WallexClient whose HTTP goes to `handler` instead of the network."""

    def _make(handler, max_retries: int = 0) -> WallexClient:
        return WallexClient(
            "https://api.test",
            "test-key",
            max_retries=max_retries,
            transport=httpx.MockTransport(handler),
            sleep=lambda _seconds: None,
        )

    return _make


@pytest.fixture
def market_routes(load_fixture):
    """Mock routes for sync_markets: both real market captures."""
    return {
        "/hector/web/v1/markets": load_fixture("markets_subset.json"),
        "/v1/markets": load_fixture("markets_v1_subset.json"),
    }


@pytest.fixture
def depth_routes(load_fixture):
    """Mock route for GET /v1/depth that serves the real capture of the requested symbol."""
    captured = load_fixture("depth_v1_allowlist.json")
    return {"/v1/depth": lambda request: captured[request.url.params["symbol"]]}
