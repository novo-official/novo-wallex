from pathlib import Path

import pyarrow.parquet as pq
import pytest

from market.orderbook_features import book_features

SAMPLE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "orderbook_sample" / "depth"


def test_simple_book():
    features = book_features([(99.0, 2.0), (98.0, 1.0)], [(101.0, 1.0), (102.0, 1.0)])
    assert features.spread_bps == pytest.approx(200.0)
    assert features.imbalance_top5 == pytest.approx((296 - 203) / (296 + 203))
    assert features.microprice_offset_bps > 0  # more size on the bid pushes the microprice up


def test_crossed_or_empty_books_give_none():
    assert book_features([], [(1.0, 1.0)]) is None
    assert book_features([(2.0, 1.0)], [(1.0, 1.0)]) is None


def test_real_snapshot_from_the_recorder_sample():
    table = pq.read_table(next(SAMPLE.rglob("*.parquet"))).to_pylist()
    first_time = min(row["captured_at"] for row in table if row["symbol"] == "BTCTMN")
    rows = [r for r in table if r["symbol"] == "BTCTMN" and r["captured_at"] == first_time]
    bids = [
        (float(r["price"]), float(r["quantity"])) for r in sorted(rows, key=lambda r: r["level"]) if r["side"] == "bid"
    ]
    asks = [
        (float(r["price"]), float(r["quantity"])) for r in sorted(rows, key=lambda r: r["level"]) if r["side"] == "ask"
    ]
    features = book_features(bids, asks)
    assert 0 < features.spread_bps < 50 and -1 <= features.imbalance_top5 <= 1
