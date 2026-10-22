from decimal import Decimal

import pydantic
import pytest

from exchange.schemas import AllMarketDepthResponse, MarketsResponse, UdfHistoryResponse


def test_markets_fixture_parses_real_response(load_fixture):
    response = MarketsResponse.model_validate(load_fixture("markets_subset.json"))
    assert response.success is True
    btc = next(market for market in response.result.markets if market.symbol == "BTCTMN")
    assert btc.price == Decimal("22075698199")
    assert btc.price_precision == 0
    assert btc.amount_precision == 8
    assert btc.quote_asset == "TMN"
    assert isinstance(btc.quote_volume_24h, Decimal)


def test_blank_or_null_market_values_become_none(load_fixture):
    raw = load_fixture("markets_subset.json")
    dead = dict(raw["result"]["markets"][0], symbol="DEADTMN", price="", volume_24h=None, quote_volume_24h=None)
    raw["result"]["markets"] = [dead]
    market = MarketsResponse.model_validate(raw).result.markets[0]
    assert (market.price, market.volume_24h, market.quote_volume_24h) == (None, None, None)


def test_udf_fixture_parses_real_response(load_fixture):
    response = UdfHistoryResponse.model_validate(load_fixture("udf_history_btctmn_60.json"))
    assert response.s == "ok"
    assert len(response.t) == 48
    assert response.open_[0] == Decimal("21295534990")
    assert isinstance(response.volume[0], Decimal)


def test_udf_rejects_arrays_of_different_length(load_fixture):
    raw = load_fixture("udf_history_btctmn_60.json")
    raw["c"] = raw["c"][:-1]
    with pytest.raises(pydantic.ValidationError):
        UdfHistoryResponse.model_validate(raw)


def test_depth_fixture_parses_levels_as_decimal(load_fixture):
    response = AllMarketDepthResponse.model_validate(load_fixture("depth_all_synthetic.json"))
    btc = response.result["BTCTMN"]
    assert btc.bid[0].price == Decimal("22080000000")
    assert btc.ask[0].quantity == Decimal("0.012")
