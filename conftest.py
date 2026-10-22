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
