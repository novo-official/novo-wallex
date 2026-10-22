from datetime import UTC, datetime
from io import StringIO

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from exchange.symbols import load_market_policy
from market import services
from tests.support import json_response


@pytest.fixture
def policy():
    return load_market_policy(settings.TRADING_CONFIG_FILE, settings.BLOCKED_MARKETS_FILE)


@pytest.mark.django_db
def test_build_universe_prints_verdicts_from_stored_data(mock_client, load_fixture, policy):
    services.sync_markets(
        mock_client(lambda request: json_response(load_fixture("markets_subset.json"))),
        policy,
    )
    services.sync_orderbook(
        mock_client(lambda request: json_response(load_fixture("depth_all_synthetic.json"))),
        policy,
        captured_at=datetime(2026, 10, 22, tzinfo=UTC),
    )
    out = StringIO()

    call_command("build_universe", stdout=out)

    lines = out.getvalue().splitlines()
    assert any(line.startswith("BTCTMN") and "ELIGIBLE" in line and "REJECTED" not in line for line in lines)
    assert any(line.startswith("DOGETMN") and "REJECTED" in line and "spread" in line for line in lines)
    assert "eligible=2 of 11 allowlisted" in lines  # BTCTMN and ETHTMN; the rest lack depth or fail the spread rule


def test_download_history_rejects_naive_timestamps_before_any_request():
    with pytest.raises(CommandError, match="timezone required"):
        call_command("download_history", "--start", "2026-10-20T05:00:00", "--end", "2026-10-21T05:00:00")


def test_download_history_rejects_blocked_market_before_any_request():
    with pytest.raises(CommandError, match="OPTMN is blocked"):
        call_command(
            "download_history",
            "--symbol",
            "OP/TMN",
            "--start",
            "2026-10-20T05:00:00+00:00",
            "--end",
            "2026-10-21T05:00:00+00:00",
        )
