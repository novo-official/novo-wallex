"""Pure helpers for OHLCV history: resolution maths, window splitting, bar parsing.

No I/O here, so the rules are unit-tested without the network or the database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from exchange.client import WallexError
from exchange.schemas import UdfHistoryResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Bar:
    open_time: datetime  # UTC, bar start
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal


def resolution_seconds(resolution: str) -> int:
    """UDF resolution in minutes ("60" -> 3600 s). Other formats are not supported yet."""
    if not resolution.isdigit() or int(resolution) <= 0:
        raise ValueError(f"unsupported resolution {resolution!r}: only positive minutes, e.g. '60'")
    return int(resolution) * 60


def floor_to_step(timestamp: int, step_seconds: int) -> int:
    """Align a Unix timestamp down to a bar boundary, so the last bar is never partial."""
    return timestamp - timestamp % step_seconds


def split_windows(start: int, end: int, step_seconds: int, chunk_bars: int) -> list[tuple[int, int]]:
    """Split [start, end) into consecutive inclusive (from, to) windows of at most chunk_bars bars.

    Each window's `to` is one second before the next window's `from`, so the
    windows do not overlap and no bar is requested twice.
    """
    if chunk_bars <= 0:
        raise ValueError("chunk_bars must be positive")
    window = step_seconds * chunk_bars
    windows: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + window, end)
        windows.append((cursor, window_end - 1))
        cursor = window_end
    return windows


def parse_bars(response: UdfHistoryResponse) -> list[Bar]:
    """Convert a UDF response to bars. Bars that break OHLC rules are logged and skipped."""
    if response.s == "no_data":
        return []
    if response.s != "ok":
        raise WallexError(f"udf/history returned status {response.s!r}")

    bars: list[Bar] = []
    for index, stamp in enumerate(response.t):
        bar = Bar(
            open_time=datetime.fromtimestamp(stamp, tz=UTC),
            open_price=response.open_[index],
            high_price=response.high[index],
            low_price=response.low[index],
            close_price=response.close[index],
            volume=response.volume[index],
        )
        if not is_consistent(bar):
            logger.warning("skipping inconsistent bar at %s: %s", bar.open_time.isoformat(), bar)
            continue
        bars.append(bar)
    return bars


def is_consistent(bar: Bar) -> bool:
    """OHLC sanity: low <= open, close <= high, all prices positive, volume not negative."""
    prices = (bar.open_price, bar.high_price, bar.low_price, bar.close_price)
    return (
        all(price > 0 for price in prices)
        and bar.volume >= 0
        and bar.low_price <= min(bar.open_price, bar.close_price)
        and bar.high_price >= max(bar.open_price, bar.close_price)
        and bar.low_price <= bar.high_price
    )
