import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market.candles_io import ManifestError, candle_path, read_candles, verify_manifest, write_candles, write_manifest

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _rows(count: int) -> list[dict]:
    return [
        {
            "open_time": T0 + timedelta(minutes=index),
            "open": Decimal("100"),
            "high": Decimal("101.5"),
            "low": Decimal("99.25"),
            "close": Decimal("100.75"),
            "volume": Decimal("0.00012345"),
        }
        for index in range(count)
    ]


def test_round_trip_keeps_exact_decimals_and_order(tmp_path):
    path = candle_path(tmp_path, "1", "BTCTMN")
    rows = _rows(5)
    assert write_candles(path, list(reversed(rows))) == 5
    loaded = read_candles(path).to_pylist()
    assert [row["open_time"] for row in loaded] == [row["open_time"] for row in rows]
    assert loaded[0]["low"] == Decimal("99.25") and loaded[0]["volume"] == Decimal("0.00012345")


def test_same_rows_give_the_same_sha256(tmp_path):
    first = candle_path(tmp_path / "a", "1", "BTCTMN")
    second = candle_path(tmp_path / "b", "1", "BTCTMN")
    write_candles(first, _rows(100))
    write_candles(second, _rows(100))
    assert first.read_bytes() == second.read_bytes()


def test_manifest_lists_files_and_detects_tampering(tmp_path):
    write_candles(candle_path(tmp_path, "1", "BTCTMN"), _rows(10))
    manifest = write_manifest(tmp_path)
    entry = manifest["files"]["candles/1m/BTCTMN.parquet"]
    assert entry["rows"] == 10 and entry["first"].startswith("2026-10-01T00:00")
    assert verify_manifest(tmp_path) == 1

    write_candles(candle_path(tmp_path, "1", "BTCTMN"), _rows(11))
    with pytest.raises(ManifestError, match="does not match"):
        verify_manifest(tmp_path)


def test_missing_file_fails_verification(tmp_path):
    write_candles(candle_path(tmp_path, "1", "BTCTMN"), _rows(3))
    write_manifest(tmp_path)
    candle_path(tmp_path, "1", "BTCTMN").unlink()
    with pytest.raises(ManifestError, match="missing"):
        verify_manifest(tmp_path)
    assert "files" in json.loads((tmp_path / "MANIFEST.json").read_text())
