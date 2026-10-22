"""Order book, public trades and ticker recorder.

Orderbook history cannot be downloaded later, so this runs from day one. It feeds
the slippage model (median recorded spread), the triangular-arbitrage study and
the shadow comparison.

Streams (Parquet, hourly, see parquet_store.py):
    depth    top N levels of /v1/depth per data-universe symbol, every depth interval
    trades   new public trades from /v1/trades (round-robin over symbols)
    tickers  bid/ask/last/24h quote volume of every market from /v1/markets
    events   one row per request: ok/error, latency, server clock offset, notes (gaps, overruns)

Symbols are ordered so that USDTTMN comes first and each TMN market is followed by
its USDT pair. That keeps the time skew between the legs of a fair-value or
triangle snapshot small (one request apart).

A failing request is recorded as an event and skipped; it never stops the loop.
A market-policy violation does stop it (fail closed).
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import pyarrow as pa
from pydantic import ValidationError

from exchange.client import WallexClient, WallexError
from exchange.schemas import MarketStats, PublicTrade, SingleMarketDepthResponse, TradesResponse
from exchange.symbols import MarketPolicy, MarketPolicyError

from .config import RecorderConfig
from .parquet_store import DECIMAL_TYPE, TIMESTAMP_TYPE, HourlyParquetStore, to_decimal128

logger = logging.getLogger(__name__)

DEPTH_SCHEMA = pa.schema(
    [
        ("captured_at", TIMESTAMP_TYPE),
        ("symbol", pa.string()),
        ("side", pa.string()),  # "bid" or "ask"
        ("level", pa.int16()),  # 0 = best price
        ("price", DECIMAL_TYPE),
        ("quantity", DECIMAL_TYPE),
    ]
)
TRADES_SCHEMA = pa.schema(
    [
        ("captured_at", TIMESTAMP_TYPE),
        ("symbol", pa.string()),
        ("trade_time", TIMESTAMP_TYPE),
        ("price", DECIMAL_TYPE),
        ("quantity", DECIMAL_TYPE),
        ("is_buy_order", pa.bool_()),
    ]
)
TICKERS_SCHEMA = pa.schema(
    [
        ("captured_at", TIMESTAMP_TYPE),
        ("symbol", pa.string()),
        ("bid", DECIMAL_TYPE),
        ("ask", DECIMAL_TYPE),
        ("last", DECIMAL_TYPE),
        ("quote_volume_24h", DECIMAL_TYPE),
    ]
)
EVENTS_SCHEMA = pa.schema(
    [
        ("captured_at", TIMESTAMP_TYPE),
        ("stream", pa.string()),
        ("symbol", pa.string()),
        ("ok", pa.bool_()),
        ("latency_ms", pa.int32()),
        ("clock_offset_ms", pa.int64()),  # local clock minus server Date header (1 s resolution)
        ("error", pa.string()),
        ("note", pa.string()),
    ]
)

TradeKey = tuple[datetime, object, object, bool]


def ordered_symbols(policy: MarketPolicy) -> list[str]:
    """USDTTMN first, then each TMN market followed by its USDT pair, then any other data symbol."""
    universe = policy.data_universe
    ordered: list[str] = ["USDTTMN"] if "USDTTMN" in universe else []
    for symbol in sorted(universe):
        if symbol.endswith("TMN") and symbol != "USDTTMN":
            ordered.append(symbol)
            pair = symbol.removesuffix("TMN") + "USDT"
            if pair in universe:
                ordered.append(pair)
    ordered += sorted(universe - set(ordered))
    for symbol in ordered:
        policy.check_data(symbol)  # fail closed at startup, before any request
    return ordered


def new_trades(trades: list[PublicTrade], previous: Counter) -> list[PublicTrade]:
    """Trades of this poll that were not in the previous poll (multiset difference, so equal trades are kept)."""
    remaining = Counter(_trade_key(trade) for trade in trades) - previous
    fresh: list[PublicTrade] = []
    for trade in trades:
        key = _trade_key(trade)
        if remaining[key] > 0:
            remaining[key] -= 1
            fresh.append(trade)
    return fresh


def _trade_key(trade: PublicTrade) -> TradeKey:
    return (trade.timestamp, trade.price, trade.quantity, trade.is_buy_order)


def clock_offset_ms(local: datetime, date_header: str | None) -> int | None:
    if not date_header:
        return None
    try:
        server = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    return int((local - server).total_seconds() * 1000)


class OrderbookRecorder:
    def __init__(
        self,
        client: WallexClient,
        policy: MarketPolicy,
        config: RecorderConfig,
        out_dir: Path,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.policy = policy
        self.config = config
        self.symbols = ordered_symbols(policy)
        self._now = now
        self._monotonic = monotonic
        self.depth = HourlyParquetStore(out_dir, "depth", DEPTH_SCHEMA)
        self.trades = HourlyParquetStore(out_dir, "trades", TRADES_SCHEMA)
        self.tickers = HourlyParquetStore(out_dir, "tickers", TICKERS_SCHEMA)
        self.events = HourlyParquetStore(out_dir, "events", EVENTS_SCHEMA)
        self._stores = (self.depth, self.trades, self.tickers, self.events)
        self._previous_trades: dict[str, Counter] = {}
        self._newest_trade: dict[str, datetime] = {}
        self._trade_cursor = 0
        self.trades_per_tick = math.ceil(
            len(self.symbols) * config.depth_interval_seconds / config.trades_interval_seconds
        )
        self._next_tickers: datetime | None = None
        self._next_flush: datetime | None = None
        self.ticks = 0

    # --- one request -----------------------------------------------------------

    def _fetch(self, stream: str, symbol: str | None, path: str, params: dict | None):
        """GET and parse; returns (payload, captured_at) or None after recording the failure as an event."""
        if symbol is not None:
            self.policy.check_data(symbol)  # before every per-symbol request
        started = self._monotonic()
        try:
            payload, response = self.client.get_json_response(path, params)
        except WallexError as exc:
            self._event(stream, symbol, ok=False, started=started, error=str(exc)[:500])
            logger.warning("%s %s failed: %s", stream, symbol or "", exc)
            return None
        captured_at = self._now()
        self._event(
            stream, symbol, ok=True, started=started, offset=clock_offset_ms(captured_at, response.headers.get("date"))
        )
        return payload, captured_at

    def _event(self, stream, symbol, *, ok, started=None, offset=None, error=None, note=None) -> None:
        latency = None if started is None else int((self._monotonic() - started) * 1000)
        self.events.append(
            [
                {
                    "captured_at": self._now(),
                    "stream": stream,
                    "symbol": symbol,
                    "ok": ok,
                    "latency_ms": latency,
                    "clock_offset_ms": offset,
                    "error": error,
                    "note": note,
                }
            ]
        )

    def _invalid(self, stream: str, symbol: str | None, exc: Exception) -> None:
        self._event(stream, symbol, ok=False, error=f"invalid response: {str(exc)[:480]}")
        logger.warning("%s %s: invalid response: %s", stream, symbol or "", exc)

    # --- streams ---------------------------------------------------------------

    def record_depth(self, symbol: str) -> None:
        fetched = self._fetch("depth", symbol, "/v1/depth", {"symbol": symbol})
        if fetched is None:
            return
        payload, captured_at = fetched
        try:
            response = SingleMarketDepthResponse.model_validate(payload)
            if not response.success:
                raise ValueError(f"success=false: {response.message}")
        except (ValidationError, ValueError) as exc:
            self._invalid("depth", symbol, exc)
            return
        rows = []
        for side, levels in (("bid", response.result.bid), ("ask", response.result.ask)):
            for index, level in enumerate(levels[: self.config.depth_levels]):
                rows.append(
                    {
                        "captured_at": captured_at,
                        "symbol": symbol,
                        "side": side,
                        "level": index,
                        "price": to_decimal128(level.price),
                        "quantity": to_decimal128(level.quantity),
                    }
                )
        self.depth.append(rows)

    def record_trades(self, symbol: str) -> None:
        fetched = self._fetch("trades", symbol, "/v1/trades", {"symbol": symbol})
        if fetched is None:
            return
        payload, captured_at = fetched
        try:
            response = TradesResponse.model_validate(payload)
            if not response.success:
                raise ValueError(f"success=false: {response.message}")
        except (ValidationError, ValueError) as exc:
            self._invalid("trades", symbol, exc)
            return
        trades = response.result.latest_trades
        previous_newest = self._newest_trade.get(symbol)
        if trades and previous_newest is not None and min(t.timestamp for t in trades) > previous_newest:
            # The whole window is newer than the last poll: trades in between were missed.
            self._event("trades", symbol, ok=True, note="possible gap: no overlap with the previous poll")
        fresh = new_trades(trades, self._previous_trades.get(symbol, Counter()))
        self._previous_trades[symbol] = Counter(_trade_key(trade) for trade in trades)
        if trades:
            self._newest_trade[symbol] = max(t.timestamp for t in trades)
        self.trades.append(
            [
                {
                    "captured_at": captured_at,
                    "symbol": symbol,
                    "trade_time": trade.timestamp,
                    "price": to_decimal128(trade.price),
                    "quantity": to_decimal128(trade.quantity),
                    "is_buy_order": trade.is_buy_order,
                }
                for trade in fresh
            ]
        )

    def record_tickers(self) -> None:
        fetched = self._fetch("tickers", None, "/v1/markets", None)
        if fetched is None:
            return
        payload, captured_at = fetched
        try:
            symbols = payload["result"]["symbols"]
            if not isinstance(symbols, dict):
                raise TypeError("result.symbols is not an object")
        except (KeyError, TypeError) as exc:
            self._invalid("tickers", None, exc)
            return
        rows, skipped = [], 0
        for symbol, info in symbols.items():
            try:
                stats = MarketStats.model_validate(info.get("stats") or {})
            except (ValidationError, AttributeError):
                skipped += 1  # one odd market must not drop the whole snapshot
                continue
            rows.append(
                {
                    "captured_at": captured_at,
                    "symbol": symbol,
                    "bid": to_decimal128(stats.bid_price),
                    "ask": to_decimal128(stats.ask_price),
                    "last": to_decimal128(stats.last_price),
                    "quote_volume_24h": to_decimal128(stats.quote_volume_24h),
                }
            )
        if skipped:
            self._event("tickers", None, ok=True, note=f"skipped {skipped} markets with invalid stats")
        self.tickers.append(rows)

    # --- scheduling --------------------------------------------------------------

    def tick(self) -> None:
        """One cycle: depth for every symbol, a round-robin slice of trades, tickers and flush when due."""
        started = self._now()
        for symbol in self.symbols:
            self.record_depth(symbol)
        for _ in range(min(self.trades_per_tick, len(self.symbols))):
            self.record_trades(self.symbols[self._trade_cursor % len(self.symbols)])
            self._trade_cursor += 1
        now = self._now()
        if self._next_tickers is None or now >= self._next_tickers:
            self.record_tickers()
            self._next_tickers = now + _seconds(self.config.tickers_interval_seconds)
        if self._next_flush is None:
            self._next_flush = started + _seconds(self.config.flush_interval_seconds)
        elif now >= self._next_flush:
            self.flush()
            self._next_flush = now + _seconds(self.config.flush_interval_seconds)
        self.ticks += 1

    def flush(self) -> None:
        now = self._now()
        for store in self._stores:
            store.flush()
            store.compact(now)

    def run(self, should_stop: Callable[[], bool], sleep: Callable[[float], None] = time.sleep) -> None:
        """Tick on wall-clock boundaries of the depth interval until `should_stop()` is true."""
        interval = self.config.depth_interval_seconds
        logger.info(
            "recorder started: %d symbols, %ss interval, %d trades polls per tick",
            len(self.symbols),
            interval,
            self.trades_per_tick,
        )
        try:
            while not should_stop():
                tick_started = time.time()
                try:
                    self.tick()
                except MarketPolicyError:
                    raise  # never keep running after a policy violation
                except Exception as exc:  # noqa: BLE001 - the recorder must survive anything else
                    logger.exception("tick failed")
                    self._event("recorder", None, ok=False, error=f"tick failed: {exc!r}"[:500])
                now = time.time()
                if now - tick_started > interval:
                    self._event("recorder", None, ok=True, note=f"tick took {now - tick_started:.1f}s > {interval}s")
                next_boundary = (math.floor(now / interval) + 1) * interval
                while not should_stop() and time.time() < next_boundary:
                    sleep(min(1.0, next_boundary - time.time()))
        finally:
            self.flush()
            logger.info("recorder stopped after %d ticks; buffers flushed", self.ticks)


def _seconds(value: int) -> timedelta:
    return timedelta(seconds=value)
