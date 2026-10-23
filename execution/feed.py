"""Live market data for the engine (GET only): 1m candles, features on the same code as the backtest,
best bid/ask and recent public trades."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.dataset import build_market_data, load_rules
from exchange.client import WallexClient
from exchange.schemas import SingleMarketDepthResponse, TradesResponse, UdfHistoryResponse
from exchange.symbols import MarketPolicy
from market.history import parse_bars
from strategy.data import load_1m
from strategy.features import add_fair_value, symbol_features


class MarketFeed:
    def __init__(
        self,
        client: WallexClient,
        policy: MarketPolicy,
        data_dir: Path,
        symbols: list[str],
        tradable: list[str],
        history_days: int,
        book_ttl: float = 10.0,
        max_quiet_minutes: int = 120,
    ) -> None:
        self.client = client
        self.policy = policy
        self.data_dir = data_dir
        self.symbols = symbols  # data universe needed by the features (TMN markets + USDT pairs)
        self.tradable = tradable
        self.history_days = history_days
        self.book_ttl = book_ttl
        self.max_quiet_minutes = max_quiet_minutes
        self.bars: dict[str, pd.DataFrame] = {}
        self.rules = load_rules(data_dir)
        self._books: dict[str, tuple[float, Decimal | None, Decimal | None]] = {}

    def load(self, now: datetime) -> None:
        start = now - timedelta(days=self.history_days)
        for symbol in self.symbols:
            path = self.data_dir / "candles" / "1m" / f"{symbol}.parquet"
            frame = load_1m(self.data_dir, symbol) if path.exists() else pd.DataFrame()
            self.bars[symbol] = frame[frame.index >= start] if not frame.empty else frame
        self.refresh(now)

    def refresh(self, now: datetime) -> int:
        """Append completed 1m bars up to `now` for every symbol (chunks of 1440 bars). Returns bars added."""
        end = int(now.timestamp()) // 60 * 60  # the bar still open at `now` is excluded
        added = 0
        for symbol in self.symbols:
            self.policy.check_data(symbol)
            frame = self.bars.get(symbol, pd.DataFrame())
            cursor = int(frame.index[-1].timestamp()) + 60 if not frame.empty else end - self.history_days * 86400
            rows = []
            while cursor < end:
                to = min(cursor + 1440 * 60, end) - 1
                bars = parse_bars(
                    UdfHistoryResponse.model_validate(self.client.get_udf_history(symbol, "1", cursor, to))
                )
                rows += [b for b in bars if cursor <= int(b.open_time.timestamp()) <= to]
                cursor = to + 1
            if rows:
                new = pd.DataFrame(
                    {
                        "open": [float(b.open_price) for b in rows],
                        "high": [float(b.high_price) for b in rows],
                        "low": [float(b.low_price) for b in rows],
                        "close": [float(b.close_price) for b in rows],
                        "volume": [float(b.volume) for b in rows],
                    },
                    index=pd.DatetimeIndex([b.open_time for b in rows], name="open_time"),
                )
                combined = pd.concat([frame, new]) if not frame.empty else new
                self.bars[symbol] = combined[~combined.index.duplicated(keep="last")].sort_index()
                added += len(rows)
        return added

    def _frontier(self) -> pd.Timestamp | None:
        """Open time of the newest 1m bar of any symbol.

        The UDF sends no bars at all for a market without trades (seen 2026-10-23: USDTTMN had no trade for 6 h at
        night while its book stayed live), so a minimum over symbols would freeze the clock. Quiet symbols are
        extended with flat zero-volume bars up to the frontier (as `fill_gaps` does inside the series); entries
        on a symbol quiet for more than `max_quiet_minutes` are refused by the engine (`stale_symbols`)."""
        lasts = [frame.index[-1] for frame in self.bars.values() if not frame.empty]
        return max(lasts) if lasts else None

    def stale_symbols(self) -> dict[str, float]:
        """Symbols whose last real bar is more than `max_quiet_minutes` older than the frontier: {symbol: minutes}."""
        frontier = self._frontier()
        if frontier is None:
            return {}
        out = {}
        for symbol, frame in self.bars.items():
            quiet = (frontier - frame.index[-1]).total_seconds() / 60 if not frame.empty else float("inf")
            if quiet > self.max_quiet_minutes:
                out[symbol] = quiet
        return out

    def _extended(self, frame: pd.DataFrame, frontier: pd.Timestamp) -> pd.DataFrame:
        if frame.empty or frame.index[-1] >= frontier:
            return frame
        index = pd.date_range(frame.index[-1] + pd.Timedelta(minutes=1), frontier, freq="1min", tz="UTC")
        close = frame["close"].iloc[-1]
        flat = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 0.0}, index=index)
        return pd.concat([frame, flat])

    def last_bar_close(self) -> pd.Timestamp | None:
        frontier = self._frontier()
        return frontier + pd.Timedelta(minutes=1) if frontier is not None else None

    def market_data(self, extra_columns=None):
        """MarketData on the 5m clock, from the same feature code as the backtest.

        `extra_columns(features) -> {symbol: {column: series}}` adds live-only columns (the ML predictions)."""
        frontier = self._frontier()
        bars = {symbol: self._extended(frame, frontier) for symbol, frame in self.bars.items()}
        features = {symbol: symbol_features(frame) for symbol, frame in bars.items() if len(frame) > 3000}
        add_fair_value(features)
        for symbol, columns in (extra_columns(features) if extra_columns else {}).items():
            for name, series in columns.items():
                if symbol in features:
                    features[symbol][name] = series.reindex(features[symbol].index)
        return build_market_data(features, self.tradable, self.rules), features

    def best_bid_ask(self, symbol: str) -> tuple[Decimal | None, Decimal | None]:
        cached = self._books.get(symbol)
        if cached and time.monotonic() - cached[0] < self.book_ttl:
            return cached[1], cached[2]
        self.policy.check_data(symbol)
        book = SingleMarketDepthResponse.model_validate(self.client.get_depth(symbol)).result
        bid = max((level.price for level in book.bid), default=None)
        ask = min((level.price for level in book.ask), default=None)
        self._books[symbol] = (time.monotonic(), bid, ask)
        return bid, ask

    def trades_since(self, symbol: str, iso_time: str) -> list[dict]:
        self.policy.check_data(symbol)
        since = datetime.fromisoformat(iso_time)
        trades = TradesResponse.model_validate(self.client.get_trades(symbol)).result.latest_trades
        return [{"price": t.price, "quantity": t.quantity, "time": t.timestamp} for t in trades if t.timestamp > since]


def latest_row_series(frame: pd.DataFrame, value: float) -> pd.Series:
    """A column that is NaN everywhere except the last row (live predictions exist only for now)."""
    series = pd.Series(np.nan, index=frame.index)
    if len(series):
        series.iloc[-1] = value
    return series


def utcnow() -> datetime:
    return datetime.now(UTC)
