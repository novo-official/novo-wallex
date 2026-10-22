"""Hourly-partitioned Parquet store for recorded market data.

Layout under `root/<stream>/`:
    YYYY-MM-DD/HH.parquet            compacted hour (UTC), sorted by time
    YYYY-MM-DD/HH/part-*.parquet     flushes of an hour that is not compacted yet

Every file is written to a temporary name and renamed, so a crash never leaves
a half-written Parquet file: at worst the rows buffered since the last flush are
lost. Compaction merges the parts of finished hours into one file and is safe to
re-run (it also merges into an existing hour file).

Money and quantities are stored as decimal128(38, 18), never as floats.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

DECIMAL_TYPE = pa.decimal128(38, 18)
TIMESTAMP_TYPE = pa.timestamp("ms", tz="UTC")
_QUANTUM = Decimal("1e-18")
_CONTEXT = Context(prec=38)  # enough for 20 integer digits plus 18 decimals
COMPRESSION = "zstd"


def to_decimal128(value: Decimal | None) -> Decimal | None:
    """Quantize to the stored scale. Wallex prices and quantities have at most 16 decimals, so nothing is lost."""
    if value is None:
        return None
    return value.quantize(_QUANTUM, context=_CONTEXT)


def hour_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class HourlyParquetStore:
    def __init__(self, root: Path, stream: str, schema: pa.Schema, time_column: str = "captured_at") -> None:
        self.directory = root / stream
        self.schema = schema
        self.time_column = time_column
        self._buffer: list[dict] = []
        self._sequence = 0

    def append(self, rows: list[dict]) -> None:
        self._buffer.extend(rows)

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def hour_file(self, hour: datetime) -> Path:
        return self.directory / hour.strftime("%Y-%m-%d") / f"{hour:%H}.parquet"

    def _parts_dir(self, hour: datetime) -> Path:
        return self.directory / hour.strftime("%Y-%m-%d") / f"{hour:%H}"

    def flush(self) -> int:
        """Write buffered rows as one part file per hour. Returns the number of rows written."""
        if not self._buffer:
            return 0
        by_hour: dict[datetime, list[dict]] = defaultdict(list)
        for row in self._buffer:
            by_hour[hour_start(row[self.time_column])].append(row)
        for hour, rows in by_hour.items():
            self._sequence += 1
            stamp = int(datetime.now(UTC).timestamp() * 1000)
            target = self._parts_dir(hour) / f"part-{stamp}-{os.getpid()}-{self._sequence:06d}.parquet"
            _write_atomic(pa.Table.from_pylist(rows, schema=self.schema), target)
        written = len(self._buffer)
        self._buffer.clear()
        return written

    def compact(self, now: datetime) -> list[Path]:
        """Merge the part files of every hour that ended before `now` into that hour's file."""
        current = hour_start(now)
        compacted: list[Path] = []
        if not self.directory.exists():
            return compacted
        for parts_dir in sorted(path for path in self.directory.glob("*/*") if path.is_dir()):
            hour = datetime.strptime(f"{parts_dir.parent.name} {parts_dir.name}", "%Y-%m-%d %H").replace(tzinfo=UTC)
            if hour >= current:
                continue
            parts = sorted(parts_dir.glob("part-*.parquet"))
            target = self.hour_file(hour)
            tables = [pq.read_table(target)] if target.exists() else []
            tables += [pq.read_table(part) for part in parts]
            if tables:
                merged = pa.concat_tables(tables).sort_by([(self.time_column, "ascending")])
                _write_atomic(merged, target)
            for part in parts:
                part.unlink()
            _remove_if_empty(parts_dir)
            compacted.append(target)
            logger.info("compacted %s: %d part files", target, len(parts))
        return compacted


def read_hours(root: Path, stream: str, start: datetime, end: datetime) -> pa.Table | None:
    """Read compacted hours and pending parts of `stream` for hours in [start, end). None when there is no data."""
    directory = root / stream
    tables: list[pa.Table] = []
    hour = hour_start(start)
    while hour < end:
        day = directory / hour.strftime("%Y-%m-%d")
        hour_file = day / f"{hour:%H}.parquet"
        if hour_file.exists():
            tables.append(pq.read_table(hour_file))
        tables += [pq.read_table(part) for part in sorted((day / f"{hour:%H}").glob("part-*.parquet"))]
        hour += timedelta(hours=1)
    return pa.concat_tables(tables) if tables else None


def _write_atomic(table: pa.Table, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    pq.write_table(table, temporary, compression=COMPRESSION)
    os.replace(temporary, target)


def _remove_if_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass
