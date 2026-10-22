from decimal import Decimal

import pydantic
import pytest

from exchange.schemas import (
    AllMarketDepthResponse,
    MarketRulesResponse,
    MarketsResponse,
    SingleMarketDepthResponse,
    TradesResponse,
    UdfHistoryResponse,
)


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


def test_single_market_depth_parses_real_capture(load_fixture):
    response = SingleMarketDepthResponse.model_validate(load_fixture("depth_v1_allowlist.json")["BTCTMN"])
    book = response.result
    assert len(book.ask) == len(book.bid) == 50
    assert book.bid[0].price == Decimal("22131272732")
    assert isinstance(book.ask[0].quantity, Decimal)
    assert [level.price for level in book.ask] == sorted(level.price for level in book.ask)  # best ask first
    assert [level.price for level in book.bid] == sorted((level.price for level in book.bid), reverse=True)


def test_depth_sum_is_level_notional_not_cumulative(load_fixture):
    book = SingleMarketDepthResponse.model_validate(load_fixture("depth_v1_allowlist.json")["ETHTMN"]).result
    for level in book.ask + book.bid:
        assert abs(level.sum - level.price * level.quantity) < Decimal("0.01")


def test_all_market_depth_envelope_accepts_real_books(load_fixture):
    # /depth/all answered 503 on every probe; this wraps real /v1/depth books in the Swagger envelope.
    captured = load_fixture("depth_v1_allowlist.json")
    raw = {"success": True, "message": "", "result": {symbol: body["result"] for symbol, body in captured.items()}}
    assert set(AllMarketDepthResponse.model_validate(raw).result) == set(captured)


def test_market_rules_parse_real_capture(load_fixture):
    symbols = MarketRulesResponse.model_validate(load_fixture("markets_v1_subset.json")).result.symbols
    btc, sol, usdt_pair = symbols["BTCTMN"], symbols["SOLTMN"], symbols["BTCUSDT"]
    assert (btc.amount_decimals, btc.price_decimals) == (8, 0)
    assert btc.min_notional == Decimal("50000") and btc.min_qty == Decimal("0.00000001")
    assert btc.market_orders_enabled and not sol.market_orders_enabled
    assert usdt_pair.min_notional == Decimal("1")
    assert btc.stats.bid_price is not None and btc.stats.bid_price < btc.stats.ask_price


def test_market_rules_blank_stats_become_none(load_fixture):
    raw = load_fixture("markets_v1_subset.json")
    raw["result"]["symbols"]["BTCTMN"]["stats"].update(bidPrice="", askPrice="-", lastPrice="-")  # both seen live
    stats = MarketRulesResponse.model_validate(raw).result.symbols["BTCTMN"].stats
    assert (stats.bid_price, stats.ask_price, stats.last_price) == (None, None, None)


def test_trades_parse_real_capture(load_fixture):
    trades = TradesResponse.model_validate(load_fixture("trades_v1_btctmn.json")).result.latest_trades
    assert len(trades) == 50
    assert all(trade.symbol == "BTCTMN" and trade.timestamp.tzinfo is not None for trade in trades)
    assert trades[0].timestamp >= trades[-1].timestamp  # newest first
