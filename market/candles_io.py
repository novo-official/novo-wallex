"""Export and import of candles as Parquet, plus the data manifest that makes runs reproducible without network.

Layout:
    data/candles/<resolution>m/<SYMBOL>.parquet   open_time + OHLCV, decimal128(38, 18), zstd, sorted
    data/MANIFEST.json                             sha256, rows and time range of every committed data file

Judges re-run the backtest from these files: `import_candles` checks every
sha256 against the manifest before loading, and the backtester reads the
Parquet files directly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .parquet_store import COMPRESSION, DECIMAL_TYPE, TIMESTAMP_TYPE, to_decimal128

CANDLE_SCHEMA = pa.schema(
    [
        ("open_time", TIMESTAMP_TYPE),  # UTC bar start
        ("open", DECIMAL_TYPE),
        ("high", DECIMAL_TYPE),
        ("low", DECIMAL_TYPE),
        ("close", DECIMAL_TYPE),
        ("volume", DECIMAL_TYPE),  # base asset
    ]
)
MANIFEST_NAME = "MANIFEST.json"


class ManifestError(Exception):
    """A data file is missing or does not match the manifest."""


def candle_path(data_dir: Path, resolution: str, symbol: str) -> Path:
    return data_dir / "candles" / f"{resolution}m" / f"{symbol}.parquet"


def write_candles(path: Path, rows: list[dict]) -> int:
    """Write rows (keys of CANDLE_SCHEMA) sorted by open_time. Returns the row count."""
    ordered = sorted(rows, key=lambda row: row["open_time"])
    table = pa.Table.from_pylist(
        [
            {
                "open_time": row["open_time"],
                **{key: to_decimal128(row[key]) for key in ("open", "high", "low", "close", "volume")},
            }
            for row in ordered
        ],
        schema=CANDLE_SCHEMA,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    # Fixed writer options keep the bytes, and so the sha256, stable for the same rows.
    pq.write_table(table, temporary, compression=COMPRESSION, write_statistics=True, row_group_size=1 << 20)
    temporary.replace(path)
    return table.num_rows


def read_candles(path: Path) -> pa.Table:
    return pq.read_table(path, schema=CANDLE_SCHEMA)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _time_range(path: Path) -> tuple[str | None, str | None]:
    metadata = pq.read_metadata(path)
    names = metadata.schema.names
    column = "open_time" if "open_time" in names else "captured_at" if "captured_at" in names else None
    if column is None or metadata.num_rows == 0:
        return None, None
    values = pq.read_table(path, columns=[column])[column]
    bounds = pc.min_max(values).as_py()
    return bounds["min"].isoformat(), bounds["max"].isoformat()


def data_files(data_dir: Path) -> list[Path]:
    """Every committed data file: the candles. Recorder output is not committed (see .gitignore)."""
    return sorted(data_dir.glob("candles/*/*.parquet"))


def write_manifest(data_dir: Path) -> dict:
    entries = {}
    for path in data_files(data_dir):
        first, last = _time_range(path)
        entries[path.relative_to(data_dir).as_posix()] = {
            "sha256": sha256_of(path),
            "bytes": path.stat().st_size,
            "rows": pq.read_metadata(path).num_rows,
            "first": first,
            "last": last,
        }
    manifest = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "source": "Wallex public API (api.wallex.ir): GET /v1/udf/history resolution 1",
        "files": entries,
    }
    (data_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify_manifest(data_dir: Path, only_prefix: str = "") -> int:
    """Check sha256 of every manifest entry under `only_prefix`. Returns the number checked; raises on mismatch."""
    manifest_path = data_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise ManifestError(f"{manifest_path} is missing")
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    checked = 0
    for relative, entry in entries.items():
        if not relative.startswith(only_prefix):
            continue
        path = data_dir / relative
        if not path.exists():
            raise ManifestError(f"{relative} is listed in the manifest but missing")
        actual = sha256_of(path)
        if actual != entry["sha256"]:
            raise ManifestError(f"{relative}: sha256 {actual} does not match the manifest {entry['sha256']}")
        checked += 1
    return checked
