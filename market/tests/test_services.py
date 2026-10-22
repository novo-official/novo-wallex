from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from django.conf import settings

from exchange.symbols import MarketBlockedError, MarketNotAllowedError, load_market_policy
from market import services
from market.config import load_market_config
from market.models import Candle, Market, OrderbookSnapshot
from tests.support import json_response

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
def test_sync_markets_stores_spot_markets_with_policy_flags(mock_client, load_fixture, policy):
    raw = load_fixture("markets_subset.json")
    client = mock_client(lambda request: json_response(raw))

    summary = services.sync_markets(client, policy)

    spot = sum(1 for market in raw["result"]["markets"] if market["is_spot"])
    assert summary.spot_markets == Market.objects.count() == spot
    btc = Market.objects.get(symbol="BTCTMN")
    assert btc.is_allowed and not btc.is_blocked
    assert btc.amount_precision == 8 and btc.price_precision == 0
    assert btc.min_order_value is None  # unknown until confirmed; execution must fail closed
    zec = Market.objects.get(symbol="ZECTMN")
    assert zec.is_blocked and not zec.is_allowed
    assert not Market.objects.get(symbol="BTCUSDT").is_allowed  # not allowlisted


@pytest.mark.django_db
def test_sync_markets_is_idempotent(mock_client, load_fixture, policy):
    raw = load_fixture("markets_subset.json")
    client = mock_client(lambda request: json_response(raw))
    services.sync_markets(client, policy)
    services.sync_markets(client, policy)
    assert Market.objects.count() == sum(1 for market in raw["result"]["markets"] if market["is_spot"])


@pytest.mark.django_db
def test_sync_orderbook_stores_top_of_book_for_allowed_markets_only(mock_client, load_fixture, policy):
    services.sync_markets(mock_client(lambda request: json_response(load_fixture("markets_subset.json"))), policy)
    depth = load_fixture("depth_all_synthetic.json")
    depth["result"]["OPTMN"] = {
        "ask": [{"price": 2, "quantity": 1, "sum": 1}],
        "bid": [{"price": 1, "quantity": 1, "sum": 1}],
    }
    client = mock_client(lambda request: json_response(depth))

    stored = services.sync_orderbook(client, policy, captured_at=datetime(2026, 10, 22, tzinfo=UTC))

    assert stored == 3  # BTCTMN, ETHTMN, DOGETMN; OPTMN is blocked and not stored
    btc = OrderbookSnapshot.objects.get(market__symbol="BTCTMN")
    assert btc.best_bid == Decimal("22080000000")
    assert btc.best_ask == Decimal("22100000000")
    assert not OrderbookSnapshot.objects.filter(market__symbol="OPTMN").exists()


@pytest.mark.django_db
def test_download_candles_stores_all_bars_of_the_real_capture(mock_client, load_fixture, policy):
    services.sync_markets(mock_client(lambda request: json_response(load_fixture("markets_subset.json"))), policy)
    client = mock_client(_udf_handler(load_fixture("udf_history_btctmn_60.json")))

    summary = services.download_candles(client, policy, "BTC/TMN", "60", START, END, chunk_bars=500)

    assert (summary.requests, summary.bars_received, summary.bars_written) == (1, 48, 48)
    assert Candle.objects.filter(market__symbol="BTCTMN", resolution="60").count() == 48
    first = Candle.objects.get(market__symbol="BTCTMN", open_time=START)
    assert first.open_price == Decimal("21295534990")
    assert first.volume == Decimal("0.13236754")


@pytest.mark.django_db
def test_download_candles_rerun_does_not_duplicate_and_updates_changed_bars(mock_client, load_fixture, policy):
    services.sync_markets(mock_client(lambda request: json_response(load_fixture("markets_subset.json"))), policy)
    udf = load_fixture("udf_history_btctmn_60.json")
    services.download_candles(mock_client(_udf_handler(udf)), policy, "BTCTMN", "60", START, END, 500)

    udf["c"][0] = "21250000000"  # the exchange corrected one bar (inside the bar range)
    services.download_candles(mock_client(_udf_handler(udf)), policy, "BTCTMN", "60", START, END, 500)

    assert Candle.objects.filter(market__symbol="BTCTMN").count() == 48
    assert Candle.objects.get(market__symbol="BTCTMN", open_time=START).close_price == Decimal("21250000000")


@pytest.mark.django_db
def test_download_candles_splits_into_chunks_without_losing_or_duplicating_bars(mock_client, load_fixture, policy):
    services.sync_markets(mock_client(lambda request: json_response(load_fixture("markets_subset.json"))), policy)
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
def test_download_refuses_market_outside_allowlist_before_any_request(mock_client, policy):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for a non-allowlisted market")

    with pytest.raises(MarketNotAllowedError):
        services.download_candles(mock_client(handler), policy, "ETHUSDT", "60", START, END, 500)


@pytest.mark.django_db
def test_universe_end_to_end_with_real_markets_and_synthetic_depth(mock_client, load_fixture, policy):
    services.sync_markets(mock_client(lambda request: json_response(load_fixture("markets_subset.json"))), policy)
    services.sync_orderbook(
        mock_client(lambda request: json_response(load_fixture("depth_all_synthetic.json"))),
        policy,
        captured_at=datetime(2026, 10, 22, tzinfo=UTC),
    )
    config = load_market_config(settings.TRADING_CONFIG_FILE)

    verdicts, missing = services.build_universe(policy, config.universe)
    by_symbol = {verdict.symbol: verdict for verdict in verdicts}

    assert missing == []
    assert by_symbol["BTCTMN"].eligible
    assert by_symbol["ETHTMN"].eligible
    assert not by_symbol["DOGETMN"].eligible  # spread about 1.9%, above 0.5%
    assert any("spread" in reason for reason in by_symbol["DOGETMN"].reasons)
    assert "no orderbook snapshot" in by_symbol["SOLTMN"].reasons  # not in the depth fixture
