from decimal import Decimal
from pathlib import Path

from backtest.costs import _book_cost, measure_slippage

SAMPLE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "orderbook_sample"


def test_book_cost_walks_levels_and_reports_thin_books():
    levels = [(100.0, 1.0), (101.0, 1.0)]
    assert _book_cost(levels, 100.0) == 100.0
    assert _book_cost(levels, 201.0) == 201.0 / (1 + 101.0 / 101.0)
    assert _book_cost(levels, 1000.0) is None


def test_slippage_from_the_recorded_sample_is_small_for_liquid_markets():
    slippage, evidence = measure_slippage(SAMPLE, ["USDTTMN", "BTCTMN", "ETHTMN"], 1_000_000, Decimal("0.0002"))
    assert set(slippage) == {"USDTTMN", "BTCTMN", "ETHTMN"}
    assert all(Decimal("0.0002") <= value < Decimal("0.002") for value in slippage.values())
    assert evidence["BTCTMN"]["snapshots"] > 40
