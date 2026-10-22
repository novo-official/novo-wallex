from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq

from market.parquet_store import DECIMAL_TYPE, TIMESTAMP_TYPE, HourlyParquetStore, read_hours, to_decimal128

SCHEMA = pa.schema([("captured_at", TIMESTAMP_TYPE), ("price", DECIMAL_TYPE)])
T0 = datetime(2026, 10, 22, 8, 59, 50, tzinfo=UTC)


def _row(moment: datetime, price: str) -> dict:
    return {"captured_at": moment, "price": to_decimal128(Decimal(price))}


def test_decimals_round_trip_exactly_for_large_prices_and_small_quantities():
    assert to_decimal128(Decimal("22162567495")) == Decimal("22162567495")
    assert to_decimal128(Decimal("0.0000225500000000")) == Decimal("0.00002255")
    assert to_decimal128(None) is None


def test_flush_splits_rows_by_hour_into_part_files(tmp_path):
    store = HourlyParquetStore(tmp_path, "depth", SCHEMA)
    store.append([_row(T0, "1"), _row(T0 + timedelta(seconds=20), "2")])  # 08:59:50 and 09:00:10

    assert store.flush() == 2

    assert len(list((tmp_path / "depth" / "2026-10-22" / "08").glob("part-*.parquet"))) == 1
    assert len(list((tmp_path / "depth" / "2026-10-22" / "09").glob("part-*.parquet"))) == 1
    assert store.buffered == 0 and store.flush() == 0


def test_compact_merges_only_finished_hours_and_is_rerunnable(tmp_path):
    store = HourlyParquetStore(tmp_path, "depth", SCHEMA)
    store.append([_row(T0, "2")])
    store.flush()
    store.append([_row(T0 - timedelta(minutes=30), "1"), _row(T0 + timedelta(minutes=5), "3")])
    store.flush()

    compacted = store.compact(now=T0 + timedelta(minutes=6))  # 09:05: hour 08 is finished, 09 is not

    hour_file = tmp_path / "depth" / "2026-10-22" / "08.parquet"
    assert compacted == [hour_file]
    assert [row["price"] for row in pq.read_table(hour_file).to_pylist()] == [Decimal("1"), Decimal("2")]  # sorted
    assert not (tmp_path / "depth" / "2026-10-22" / "08").exists()
    assert len(list((tmp_path / "depth" / "2026-10-22" / "09").glob("part-*.parquet"))) == 1

    store.append([_row(T0 - timedelta(minutes=1), "1.5")])  # a late row for hour 08 merges into the hour file
    store.flush()
    store.compact(now=T0 + timedelta(minutes=7))
    assert pq.read_table(hour_file).num_rows == 3


def test_read_hours_returns_compacted_and_pending_rows(tmp_path):
    store = HourlyParquetStore(tmp_path, "depth", SCHEMA)
    store.append([_row(T0, "1"), _row(T0 + timedelta(minutes=1), "2")])
    store.flush()
    store.compact(now=T0 + timedelta(minutes=2))

    table = read_hours(tmp_path, "depth", T0 - timedelta(hours=1), T0 + timedelta(hours=2))

    assert table.num_rows == 2
    assert read_hours(tmp_path, "depth", T0 + timedelta(hours=5), T0 + timedelta(hours=6)) is None


def test_no_temporary_files_are_left_behind(tmp_path):
    store = HourlyParquetStore(tmp_path, "depth", SCHEMA)
    store.append([_row(T0, "1")])
    store.flush()
    store.compact(now=T0 + timedelta(hours=1))
    assert not list(tmp_path.rglob("*.tmp"))
