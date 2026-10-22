from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from django.conf import settings

from exchange.symbols import MarketBlockedError, MarketNotInDataUniverseError, load_market_policy
from market import services
from market.config import load_market_config
from market.models import Candle, Market, OrderbookSnapshot
from tests.support import json_response, route

START = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
END = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)


@pytest.fixture
def policy():
    return load_market_policy(settings.TRADING_CONFIG_FILE, settings.BLOCKED_MARKETS_FILE)


def _udf_handler(udf_response: dict, *, filter_by_window: bool = False):
    """Serve the real UDF capture. With filter_by_window, behave like the server: return bars in [from, to]."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/udf/history"
        body = udf_response
        if filter_by_window:
            low, high = int(request.url.params["from"]), int(request.url.params["to"])
            keep = [i for i, stamp in enumerate(udf_response["t"]) if low <= stamp <= high]
            body = {
                key: [values[i] for i in keep] if isinstance(values, list) else values
                for key, values in udf_response.items()
            }
        return json_response(body)

    return handler


@pytest.mark.django_db
def test_sync_markets_stores_spot_markets_with_policy_flags(mock_client, market_routes, policy):
    summary = services.sync_markets(mock_client(route(market_routes)), policy)

    raw = market_routes["/hector/web/v1/markets"]
    spot = sum(1 for market in raw["result"]["markets"] if market["is_spot"])
    assert summary.spot_markets == Market.objects.count() == spot
    btc = Market.objects.get(symbol="BTCTMN")
    assert btc.is_allowed and not btc.is_blocked
    assert btc.amount_precision == 8 and btc.price_precision == 0
    zec = Market.objects.get(symbol="ZECTMN")
    assert zec.is_blocked and not zec.is_allowed
    assert not Market.objects.get(symbol="BTCUSDT").is_allowed  # not allowlisted


@pytest.mark.django_db
def test_sync_markets_stores_exchange_minimums_and_market_order_flag(mock_client, market_routes, policy):
    services.sync_markets(mock_client(route(market_routes)), policy)

    btc = Market.objects.get(symbol="BTCTMN")
    assert btc.min_order_value == Decimal("50000")  # TMN, from /v1/markets minNotional
    assert btc.min_qty == Decimal("0.00000001")
    assert btc.market_orders_enabled is True
    sol = Market.objects.get(symbol="SOLTMN")
    assert sol.market_orders_enabled is False  # exits on SOLTMN must use limit orders
    assert Market.objects.get(symbol="BTCUSDT").min_order_value == Decimal("1")  # USDT


@pytest.mark.django_db
def test_market_without_rules_keeps_null_minimum(mock_client, market_routes, policy):
    del market_routes["/v1/markets"]["result"]["symbols"]["ETHTMN"]

    summary = services.sync_markets(mock_client(route(market_routes)), policy)

    eth = Market.objects.get(symbol="ETHTMN")
    assert eth.min_order_value is None and eth.min_qty is None  # execution must refuse (fail closed)
    assert eth.market_orders_enabled is False
    assert summary.without_rules == 1


@pytest.mark.django_db
def test_precision_conflict_between_endpoints_disallows_market(mock_client, market_routes, policy):
    market_routes["/v1/markets"]["result"]["symbols"]["BTCTMN"]["stepSize"] = 6

    services.sync_markets(mock_client(route(market_routes)), policy)

    assert not Market.objects.get(symbol="BTCTMN").is_allowed
    assert Market.objects.get(symbol="ETHTMN").is_allowed


@pytest.mark.django_db
def test_sync_markets_is_idempotent(mock_client, market_routes, policy):
    client = mock_client(route(market_routes))
    services.sync_markets(client, policy)
    services.sync_markets(client, policy)
    raw = market_routes["/hector/web/v1/markets"]
    assert Market.objects.count() == sum(1 for market in raw["result"]["markets"] if market["is_spot"])


@pytest.mark.django_db
def test_sync_orderbook_requests_only_allowlisted_markets(mock_client, market_routes, depth_routes, policy):
    services.sync_markets(mock_client(route(market_routes)), policy)
    requested: list[str] = []

    def depth(request: httpx.Request):
        requested.append(request.url.params["symbol"])
        return depth_routes["/v1/depth"](request)

    stored = services.sync_orderbook(
        mock_client(route({"/v1/depth": depth})), policy, datetime(2026, 10, 22, tzinfo=UTC)
    )

    assert stored == 11
    assert sorted(requested) == sorted(policy.allowlist)  # never a blocked or non-allowlisted market
    btc = OrderbookSnapshot.objects.get(market__symbol="BTCTMN")
    assert btc.best_bid == Decimal("22131272732")  # real capture, 2026-10-22
    assert btc.best_ask == Decimal("22155918712")


@pytest.mark.django_db
def test_sync_orderbook_skips_a_failing_market_and_continues(mock_client, market_routes, depth_routes, policy):
    services.sync_markets(mock_client(route(market_routes)), policy)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["symbol"] == "ETHTMN":
            return httpx.Response(503, text="<html>unavailable</html>")
        return json_response(depth_routes["/v1/depth"](request))

    stored = services.sync_orderbook(mock_client(handler), policy, datetime(2026, 10, 22, tzinfo=UTC))

    assert stored == 10
    assert not OrderbookSnapshot.objects.filter(market__symbol="ETHTMN").exists()


@pytest.mark.django_db
def test_download_candles_stores_all_bars_of_the_real_capture(mock_client, load_fixture, market_routes, policy):
    services.sync_markets(mock_client(route(market_routes)), policy)
    client = mock_client(_udf_handler(load_fixture("udf_history_btctmn_60.json")))

    summary = services.download_candles(client, policy, "BTC/TMN", "60", START, END, chunk_bars=500)

    assert (summary.requests, summary.bars_received, summary.bars_written) == (1, 48, 48)
    assert Candle.objects.filter(market__symbol="BTCTMN", resolution="60").count() == 48
    first = Candle.objects.get(market__symbol="BTCTMN", open_time=START)
    assert first.open_price == Decimal("21295534990")
    assert first.volume == Decimal("0.13236754")


@pytest.mark.django_db
def test_download_candles_rerun_does_not_duplicate_and_updates_changed_bars(
    mock_client, load_fixture, market_routes, policy
):
    services.sync_markets(mock_client(route(market_routes)), policy)
    udf = load_fixture("udf_history_btctmn_60.json")
    services.download_candles(mock_client(_udf_handler(udf)), policy, "BTCTMN", "60", START, END, 500)

    udf["c"][0] = "21250000000"  # the exchange corrected one bar (inside the bar range)
    services.download_candles(mock_client(_udf_handler(udf)), policy, "BTCTMN", "60", START, END, 500)

    assert Candle.objects.filter(market__symbol="BTCTMN").count() == 48
    assert Candle.objects.get(market__symbol="BTCTMN", open_time=START).close_price == Decimal("21250000000")


@pytest.mark.django_db
def test_download_candles_splits_into_chunks_without_losing_or_duplicating_bars(
    mock_client, load_fixture, market_routes, policy
):
    services.sync_markets(mock_client(route(market_routes)), policy)
    client = mock_client(_udf_handler(load_fixture("udf_history_btctmn_60.json"), filter_by_window=True))

    summary = services.download_candles(client, policy, "BTCTMN", "60", START, END, chunk_bars=10)

    assert summary.requests == 5
    assert summary.bars_received == 48
    assert Candle.objects.filter(market__symbol="BTCTMN").count() == 48


@pytest.mark.django_db
def test_download_refuses_blocked_market_before_any_request(mock_client, policy):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for a blocked market")

    with pytest.raises(MarketBlockedError):
        services.download_candles(mock_client(handler), policy, "OP/TMN", "60", START, END, 500)


@pytest.mark.django_db
def test_download_refuses_market_outside_data_universe_before_any_request(mock_client, policy):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for a market outside the data universe")

    with pytest.raises(MarketNotInDataUniverseError):
        services.download_candles(mock_client(handler), policy, "XLMTMN", "60", START, END, 500)


@pytest.mark.django_db
def test_download_flags_a_truncated_window(mock_client, load_fixture, market_routes, policy):
    services.sync_markets(mock_client(route(market_routes)), policy)
    udf = load_fixture("udf_history_btctmn_60.json")
    single = {key: values[:1] if isinstance(values, list) else values for key, values in udf.items()}

    summary = services.download_candles(mock_client(_udf_handler(single)), policy, "BTCTMN", "1", START, END, 1440)

    assert summary.requests == 2 and summary.suspicious_windows == 2  # 2 x 1440 expected, 1 or 0 received


@pytest.mark.django_db
def test_universe_end_to_end_with_real_captures(mock_client, market_routes, depth_routes, policy):
    services.sync_markets(mock_client(route(market_routes)), policy)
    services.sync_orderbook(mock_client(route(depth_routes)), policy, captured_at=datetime(2026, 10, 22, tzinfo=UTC))
    config = load_market_config(settings.TRADING_CONFIG_FILE)

    verdicts, missing = services.build_universe(policy, config.universe)
    by_symbol = {verdict.symbol: verdict for verdict in verdicts}

    assert missing == []
    assert by_symbol["BTCTMN"].eligible
    assert by_symbol["ETHTMN"].eligible
    assert not by_symbol["SHIBTMN"].eligible  # spread about 1.05% in this capture, above 0.5%
    assert any("spread" in reason for reason in by_symbol["SHIBTMN"].reasons)
    assert sum(verdict.eligible for verdict in verdicts) == 10


@pytest.mark.django_db
def test_universe_rejects_market_without_snapshot(mock_client, market_routes, depth_routes, policy):
    services.sync_markets(mock_client(route(market_routes)), policy)
    captured = depth_routes["/v1/depth"]
    without_sol = {
        "/v1/depth": lambda request: (
            {"success": False, "message": "x", "result": {"ask": [], "bid": []}}
            if request.url.params["symbol"] == "SOLTMN"
            else captured(request)
        )
    }
    services.sync_orderbook(mock_client(route(without_sol)), policy, captured_at=datetime(2026, 10, 22, tzinfo=UTC))

    verdicts, _ = services.build_universe(policy, load_market_config(settings.TRADING_CONFIG_FILE).universe)

    assert "no orderbook snapshot" in {verdict.symbol: verdict for verdict in verdicts}["SOLTMN"].reasons
