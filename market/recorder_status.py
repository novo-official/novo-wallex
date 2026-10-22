"""Summary of recorded data: coverage, request errors and spreads. Pure reads of the Parquet files."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.compute as pc

from .parquet_store import read_hours


@dataclass(frozen=True)
class SymbolStatus:
    symbol: str
    snapshots: int
    coverage: float  # snapshots / expected snapshots in the window
    median_spread_bps: Decimal | None
    p90_spread_bps: Decimal | None


@dataclass(frozen=True)
class RecorderStatus:
    start: datetime
    end: datetime
    requests: int
    errors: int
    notes: dict[str, int]
    median_latency_ms: float | None
    median_clock_offset_ms: float | None
    trades: int
    ticker_snapshots: int
    symbols: list[SymbolStatus]


def _quantile(values: list[Decimal], fraction: float) -> Decimal:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def summarize(root: Path, start: datetime, end: datetime, depth_interval_seconds: int) -> RecorderStatus:
    events = read_hours(root, "events", start, end)
    event_rows = events.to_pylist() if events is not None else []
    event_rows = [row for row in event_rows if start <= row["captured_at"] < end]
    request_rows = [row for row in event_rows if row["latency_ms"] is not None or not row["ok"]]
    notes: dict[str, int] = {}
    for row in event_rows:
        if row["note"]:
            key = row["note"].split(":")[0]
            notes[key] = notes.get(key, 0) + 1
    latencies = [row["latency_ms"] for row in event_rows if row["latency_ms"] is not None]
    offsets = [row["clock_offset_ms"] for row in event_rows if row["clock_offset_ms"] is not None]

    expected = max(1, int((end - start).total_seconds() // depth_interval_seconds))
    symbols: list[SymbolStatus] = []
    depth = read_hours(root, "depth", start, end)
    if depth is not None:
        best = depth.filter(pc.equal(depth["level"], 0)).to_pylist()
        books: dict[tuple[str, datetime], dict[str, Decimal]] = {}
        for row in best:
            if start <= row["captured_at"] < end:
                books.setdefault((row["symbol"], row["captured_at"]), {})[row["side"]] = row["price"]
        spreads: dict[str, list[Decimal]] = {}
        counts: dict[str, int] = {}
        for (symbol, _), sides in books.items():
            counts[symbol] = counts.get(symbol, 0) + 1
            bid, ask = sides.get("bid"), sides.get("ask")
            if bid and ask and ask >= bid:
                spreads.setdefault(symbol, []).append((ask - bid) / ((ask + bid) / 2) * 10000)
        for symbol in sorted(counts):
            values = spreads.get(symbol, [])
            symbols.append(
                SymbolStatus(
                    symbol=symbol,
                    snapshots=counts[symbol],
                    coverage=counts[symbol] / expected,
                    median_spread_bps=statistics.median(values) if values else None,
                    p90_spread_bps=_quantile(values, 0.9) if values else None,
                )
            )

    trades = read_hours(root, "trades", start, end)
    tickers = read_hours(root, "tickers", start, end)
    return RecorderStatus(
        start=start,
        end=end,
        requests=len(request_rows),
        errors=sum(1 for row in request_rows if not row["ok"]),
        notes=notes,
        median_latency_ms=statistics.median(latencies) if latencies else None,
        median_clock_offset_ms=statistics.median(offsets) if offsets else None,
        trades=trades.num_rows if trades is not None else 0,
        ticker_snapshots=len(set(tickers["captured_at"].to_pylist())) if tickers is not None else 0,
        symbols=symbols,
    )
