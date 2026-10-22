"""Trading costs: fees and slippage, the latter measured on the recorded order book.

Slippage of a taker fill = half the spread + the extra price impact of walking
the book for the order size, both as a fraction of mid. We take the median over
all recorded snapshots and never go below `floor`. Maker fills at our own limit
price have no slippage, only the maker fee.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.compute as pc

from market.parquet_store import read_hours


@dataclass(frozen=True)
class Fees:
    maker: Decimal
    taker: Decimal

    def stressed(self, multiplier: Decimal) -> Fees:
        return Fees(self.maker * multiplier, self.taker * multiplier)


@dataclass(frozen=True)
class CostModel:
    fees: Fees  # TMN-quoted markets; every traded market is TMN-quoted
    slippage: dict[str, Decimal] = field(default_factory=dict)  # taker slippage per symbol, fraction of price
    default_slippage: Decimal = Decimal("0.002")

    def slippage_for(self, symbol: str) -> Decimal:
        return self.slippage.get(symbol, self.default_slippage)


def _book_cost(levels: list[tuple[float, float]], notional: float) -> float | None:
    """Average price paid for `notional` (quote) walking `levels` (price, quantity), or None if too thin."""
    remaining, spent, bought = notional, 0.0, 0.0
    for price, quantity in levels:
        take = min(remaining, price * quantity)
        spent += take
        bought += take / price
        remaining -= take
        if remaining <= 1e-9:
            return spent / bought
    return None


def measure_slippage(
    orderbook_dir: Path, symbols: list[str], notional_tmn: float, floor: Decimal, start: datetime | None = None
) -> tuple[dict[str, Decimal], dict[str, dict]]:
    """Median taker slippage per symbol (buy and sell side averaged), plus the evidence behind each number."""
    start = start or datetime(2020, 1, 1, tzinfo=UTC)
    table = read_hours(orderbook_dir, "depth", start, datetime.now(UTC))
    result: dict[str, Decimal] = {}
    evidence: dict[str, dict] = {}
    if table is None:
        return result, evidence
    for symbol in symbols:
        rows = table.filter(pc.equal(table["symbol"], symbol)).to_pylist()
        books: dict[datetime, dict[str, list[tuple[float, float]]]] = {}
        for row in rows:
            side = books.setdefault(row["captured_at"], {"bid": [], "ask": []})[row["side"]]
            side.append((row["level"], float(row["price"]), float(row["quantity"])))
        costs: list[float] = []
        thin = 0
        for book in books.values():
            bids = [(p, q) for _, p, q in sorted(book["bid"])]
            asks = [(p, q) for _, p, q in sorted(book["ask"])]
            if not bids or not asks:
                continue
            mid = (bids[0][0] + asks[0][0]) / 2
            # Selling base worth `notional` at mid walks the bids; buying walks the asks.
            buy = _book_cost(asks, notional_tmn)
            sell = _book_cost(bids, notional_tmn)
            if buy is None or sell is None:
                thin += 1
                continue
            costs.append(((buy - mid) / mid + (mid - sell) / mid) / 2)
        if costs:
            median = Decimal(str(round(statistics.median(costs), 6)))
            result[symbol] = max(floor, median)
            evidence[symbol] = {"snapshots": len(costs), "too_thin": thin, "median": float(median)}
    return result, evidence
