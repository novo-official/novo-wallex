from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from django.conf import settings

from exchange.schemas import TradesResponse
from exchange.symbols import MarketBlockedError, MarketPolicy, load_market_policy
from market.config import RecorderConfig, load_market_config
from market.parquet_store import read_hours
from market.recorder import OrderbookRecorder, clock_offset_ms, new_trades, ordered_symbols
from tests.support import json_response

NOW = datetime(2026, 10, 22, 8, 36, 40, tzinfo=UTC)
CONFIG = RecorderConfig(
    out_dir="unused",
    depth_interval_seconds=10,
    depth_levels=20,
    trades_interval_seconds=30,
    tickers_interval_seconds=60,
    flush_interval_seconds=60,
    max_requests_per_second=100.0,
)


@pytest.fixture
def policy():
    return load_market_policy(settings.TRADING_CONFIG_FILE, settings.BLOCKED_MARKETS_FILE)


@pytest.fixture
def api(load_fixture):
    """Serve the real captures; any symbol without a depth capture (the USDT pairs) gets BTCTMN's book."""
    depth = load_fixture("depth_v1_allowlist.json")
    trades = load_fixture("trades_v1_btctmn.json")
    markets = load_fixture("markets_v1_subset.json")
    requested: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params.get("symbol")
        requested.append((request.url.path, symbol))
        if request.url.path == "/v1/depth":
            body = depth.get(symbol, depth["BTCTMN"])
        elif request.url.path == "/v1/trades":
            body = trades
        elif request.url.path == "/v1/markets":
            body = markets
        else:
            raise AssertionError(f"unexpected request {request.url}")
        response = json_response(body)
        response.headers["date"] = "Thu, 22 Oct 2026 08:36:40 GMT"
        return response

    handler.requested = requested
    return handler


def _recorder(mock_client, handler, policy, tmp_path):
    return OrderbookRecorder(mock_client(handler), policy, CONFIG, tmp_path, now=lambda: NOW)


def test_repository_data_universe_is_allowlist_plus_usdt_pairs(policy):
    assert len(policy.data_universe) == 21
    assert {"BTCUSDT", "SHIBUSDT"} <= policy.data_universe
    assert not policy.is_tradable("BTCUSDT")  # data only: never traded


def test_symbols_are_ordered_so_each_tmn_market_is_next_to_its_usdt_pair(policy):
    symbols = ordered_symbols(policy)
    assert symbols[0] == "USDTTMN"
    assert symbols[symbols.index("BTCTMN") + 1] == "BTCUSDT"
    assert sorted(symbols) == sorted(policy.data_universe)


def test_data_universe_with_a_blocked_market_fails_closed():
    with pytest.raises(Exception, match="blocked"):
        MarketPolicy(allowlist=frozenset({"BTCTMN"}), blocked=frozenset({"OPTMN"}), data_symbols=frozenset({"OPTMN"}))


def test_check_data_rejects_blocked_and_unknown_symbols(policy):
    policy.check_data("BTCUSDT")
    with pytest.raises(MarketBlockedError):
        policy.check_data("OP/TMN")
    with pytest.raises(Exception, match="not in the data universe"):
        policy.check_data("XLMTMN")


def test_tick_records_depth_for_every_symbol_and_only_data_universe_requests(mock_client, api, policy, tmp_path):
    recorder = _recorder(mock_client, api, policy, tmp_path)

    recorder.tick()
    recorder.flush()

    depth = read_hours(tmp_path, "depth", NOW - timedelta(hours=1), NOW + timedelta(hours=1)).to_pylist()
    assert {row["symbol"] for row in depth} == policy.data_universe
    btc = [row for row in depth if row["symbol"] == "BTCTMN"]
    assert len(btc) == 40  # 20 levels per side
    best_bid = next(row for row in btc if row["side"] == "bid" and row["level"] == 0)
    assert best_bid["price"] == Decimal("22131272732")
    symbols_requested = {symbol for _, symbol in api.requested if symbol}
    assert symbols_requested <= policy.data_universe
    assert ("/v1/markets", None) in api.requested  # tickers on the first tick


def test_trades_are_round_robin_and_tickers_cover_all_markets(mock_client, api, policy, tmp_path):
    recorder = _recorder(mock_client, api, policy, tmp_path)
    assert recorder.trades_per_tick == 7  # 21 symbols, each polled every 30 s with a 10 s tick

    recorder.tick()
    recorder.flush()

    trade_polls = [symbol for path, symbol in api.requested if path == "/v1/trades"]
    assert trade_polls == ordered_symbols(policy)[:7]
    tickers = read_hours(tmp_path, "tickers", NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert tickers.num_rows == 24  # every market in the /v1/markets capture


def test_a_failing_request_is_an_event_and_the_tick_continues(mock_client, api, policy, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("symbol") == "ETHTMN":
            return httpx.Response(503, text="<html>unavailable</html>")
        return api(request)

    recorder = _recorder(mock_client, handler, policy, tmp_path)
    recorder.tick()
    recorder.flush()

    window = (NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    depth_symbols = {row["symbol"] for row in read_hours(tmp_path, "depth", *window).to_pylist()}
    assert depth_symbols == policy.data_universe - {"ETHTMN"}
    errors = [row for row in read_hours(tmp_path, "events", *window).to_pylist() if not row["ok"]]
    assert [(row["stream"], row["symbol"]) for row in errors] == [("depth", "ETHTMN")]
    assert "503" in errors[0]["error"]


def test_invalid_payload_is_recorded_not_raised(mock_client, api, policy, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("symbol") == "BTCTMN" and request.url.path == "/v1/depth":
            return json_response({"success": True, "result": {"ask": [{"price": "abc"}]}})
        return api(request)

    recorder = _recorder(mock_client, handler, policy, tmp_path)
    recorder.tick()
    recorder.flush()

    events = read_hours(tmp_path, "events", NOW - timedelta(hours=1), NOW + timedelta(hours=1)).to_pylist()
    assert any(row["symbol"] == "BTCTMN" and (row["error"] or "").startswith("invalid response") for row in events)


def test_new_trades_uses_multiset_difference(load_fixture):
    trades = TradesResponse.model_validate(load_fixture("trades_v1_btctmn.json")).result.latest_trades
    previous = Counter((t.timestamp, t.price, t.quantity, t.is_buy_order) for t in trades[10:])

    assert new_trades(trades, previous) == trades[:10]
    assert new_trades(trades, Counter()) == trades
    repeated = trades[:5] + trades[:1]  # the same trade twice in this poll, once in the previous one
    previous_once = Counter([(trades[0].timestamp, trades[0].price, trades[0].quantity, trades[0].is_buy_order)])
    assert len(new_trades(repeated, previous_once)) == 5


def test_second_trades_poll_writes_only_new_trades(mock_client, api, policy, tmp_path):
    recorder = _recorder(mock_client, api, policy, tmp_path)
    recorder.record_trades("BTCTMN")
    recorder.record_trades("BTCTMN")  # identical response: nothing new
    recorder.flush()

    trades = read_hours(tmp_path, "trades", NOW - timedelta(days=1), NOW + timedelta(hours=1))
    assert trades.num_rows == 50


def test_blocked_symbol_is_never_requested(mock_client, policy, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent for a blocked market")

    recorder = OrderbookRecorder(mock_client(handler), policy, CONFIG, tmp_path, now=lambda: NOW)
    with pytest.raises(MarketBlockedError):
        recorder.record_depth("OPTMN")


def test_clock_offset_from_date_header():
    assert clock_offset_ms(NOW + timedelta(milliseconds=400), "Thu, 22 Oct 2026 08:36:40 GMT") == 400
    assert clock_offset_ms(NOW, None) is None
    assert clock_offset_ms(NOW, "not a date") is None


def test_run_stops_and_flushes(mock_client, api, policy, tmp_path):
    recorder = _recorder(mock_client, api, policy, tmp_path)
    recorder.run(lambda: recorder.ticks >= 1, sleep=lambda seconds: None)
    assert read_hours(tmp_path, "depth", NOW - timedelta(hours=1), NOW + timedelta(hours=1)).num_rows > 0


def test_repository_recorder_config_loads():
    recorder = load_market_config(settings.TRADING_CONFIG_FILE).recorder
    assert recorder.depth_interval_seconds == 10 and recorder.depth_levels == 20


def test_status_summarizes_coverage_errors_and_spreads(mock_client, api, policy, tmp_path):
    from market.recorder_status import summarize

    recorder = _recorder(mock_client, api, policy, tmp_path)
    recorder.tick()
    recorder.flush()

    status = summarize(tmp_path, NOW - timedelta(seconds=10), NOW + timedelta(seconds=1), 10)

    assert status.errors == 0 and status.requests == 21 + 7 + 1  # depth, trades slice, tickers
    btc = next(row for row in status.symbols if row.symbol == "BTCTMN")
    assert btc.snapshots == 1
    # real capture: bid 22131272732, ask 22155918712 -> about 11.1 bps
    assert Decimal("11.1") < btc.median_spread_bps < Decimal("11.2")
