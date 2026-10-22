from datetime import UTC, datetime
from decimal import Decimal

import pytest

from exchange.client import WallexError
from exchange.schemas import UdfHistoryResponse
from market.history import Bar, floor_to_step, is_consistent, parse_bars, resolution_seconds, split_windows


def test_resolution_is_minutes_to_seconds():
    assert resolution_seconds("60") == 3600
    assert resolution_seconds("5") == 300


@pytest.mark.parametrize("bad", ["", "0", "D", "1D", "-5", "1.5"])
def test_resolution_rejects_unsupported_formats(bad):
    with pytest.raises(ValueError):
        resolution_seconds(bad)


def test_floor_to_step_aligns_down():
    assert floor_to_step(7205, 3600) == 7200
    assert floor_to_step(7200, 3600) == 7200


def test_split_windows_covers_range_without_overlap():
    windows = split_windows(start=0, end=10 * 3600, step_seconds=3600, chunk_bars=4)
    assert windows == [(0, 14399), (14400, 28799), (28800, 35999)]


def test_split_windows_empty_range_gives_no_windows():
    assert split_windows(start=100, end=100, step_seconds=60, chunk_bars=5) == []


def test_split_windows_rejects_non_positive_chunk():
    with pytest.raises(ValueError):
        split_windows(start=0, end=100, step_seconds=60, chunk_bars=0)


def test_parse_bars_from_real_capture(load_fixture):
    bars = parse_bars(UdfHistoryResponse.model_validate(load_fixture("udf_history_btctmn_60.json")))
    assert len(bars) == 48
    assert bars[0].open_time == datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
    gaps = {(b.open_time - a.open_time).total_seconds() for a, b in zip(bars, bars[1:])}
    assert gaps == {3600.0}
    assert all(isinstance(bar.close_price, Decimal) for bar in bars)


def test_no_data_status_gives_no_bars():
    assert parse_bars(UdfHistoryResponse.model_validate({"s": "no_data"})) == []


def test_error_status_raises():
    with pytest.raises(WallexError):
        parse_bars(UdfHistoryResponse.model_validate({"s": "error"}))


def test_inconsistent_bar_is_skipped():
    response = UdfHistoryResponse.model_validate(
        {
            "s": "ok",
            "t": [1_700_000_000, 1_700_003_600],
            "o": ["100", "100"],
            "h": ["110", "99"],  # second bar: high below open
            "l": ["90", "90"],
            "c": ["105", "101"],
            "v": ["1", "1"],
        }
    )
    bars = parse_bars(response)
    assert [bar.open_time.timestamp() for bar in bars] == [1_700_000_000]


@pytest.mark.parametrize(
    "bar",
    [
        Bar(datetime(2026, 1, 1, tzinfo=UTC), Decimal("0"), Decimal("1"), Decimal("0"), Decimal("1"), Decimal("1")),
        Bar(datetime(2026, 1, 1, tzinfo=UTC), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("-1")),
        Bar(datetime(2026, 1, 1, tzinfo=UTC), Decimal("2"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1")),
    ],
)
def test_is_consistent_rejects_bad_ohlc(bar):
    assert not is_consistent(bar)
