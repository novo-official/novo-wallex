from decimal import Decimal

import pytest
from django.conf import settings

from market.config import load_market_config
from market.universe import Candidate, UniverseConfig, evaluate, spread_fraction

CONFIG = UniverseConfig(
    quote_asset="TMN",
    min_quote_volume_24h=Decimal("1000000000"),
    max_spread_fraction=Decimal("0.005"),
)


def _candidate(**overrides) -> Candidate:
    values = {
        "symbol": "BTCTMN",
        "quote_asset": "TMN",
        "policy_error": None,
        "quote_volume_24h": Decimal("115000000000"),
        "spread_fraction": Decimal("0.001"),
    }
    values.update(overrides)
    return Candidate(**values)


def test_spread_fraction_uses_mid_price():
    assert spread_fraction(Decimal("100"), Decimal("101")) == Decimal("1") / Decimal("100.5")


@pytest.mark.parametrize("bid, ask", [("0", "101"), ("100", "0"), ("101", "100")])
def test_spread_fraction_rejects_invalid_top_of_book(bid, ask):
    with pytest.raises(ValueError):
        spread_fraction(Decimal(bid), Decimal(ask))


def test_healthy_market_is_eligible():
    (verdict,) = evaluate([_candidate()], CONFIG)
    assert verdict.eligible
    assert verdict.reasons == ()


@pytest.mark.parametrize(
    "overrides, expected_reason",
    [
        ({"policy_error": "BTCTMN is blocked"}, "BTCTMN is blocked"),
        ({"quote_asset": "USDT"}, "quote asset USDT is not TMN"),
        ({"quote_volume_24h": None}, "no 24h volume data"),
        ({"quote_volume_24h": Decimal("999999999")}, "below"),
        ({"spread_fraction": None}, "no orderbook snapshot"),
        ({"spread_fraction": Decimal("0.006")}, "above"),
    ],
)
def test_each_rule_rejects_with_a_reason(overrides, expected_reason):
    (verdict,) = evaluate([_candidate(**overrides)], CONFIG)
    assert not verdict.eligible
    assert any(expected_reason in reason for reason in verdict.reasons)


def test_every_failing_rule_is_reported():
    (verdict,) = evaluate([_candidate(quote_volume_24h=Decimal("1"), spread_fraction=Decimal("1"))], CONFIG)
    assert len(verdict.reasons) == 2


def test_repository_universe_config_loads():
    config = load_market_config(settings.TRADING_CONFIG_FILE)
    assert config.universe.quote_asset == "TMN"
    assert config.universe.min_quote_volume_24h == Decimal("1000000000")
    assert config.universe.max_spread_fraction == Decimal("0.005")
    assert config.chunk_bars == 1440
