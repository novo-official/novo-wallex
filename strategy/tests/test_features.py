"""Lookahead test: features computed on data truncated at T equal the full-history features at T."""

import numpy as np
import pandas as pd
import pytest

from strategy.data import fill_gaps, resample
from strategy.features import add_fair_value, symbol_features

START = pd.Timestamp("2026-09-01T00:00:00Z")


def _random_walk(seed: int, minutes: int = 12 * 24 * 60, start_price: float = 1000.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = start_price * np.exp(np.cumsum(rng.normal(0, 0.001, minutes)))
    open_ = np.concatenate([[start_price], close[:-1]])
    spread = np.abs(rng.normal(0, 0.0007, minutes)) * close
    volume = np.where(rng.random(minutes) < 0.3, 0.0, rng.random(minutes))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    flat = volume == 0  # zero-volume bars are flat, as on Wallex
    high, low, open_ = np.where(flat, close, high), np.where(flat, close, low), np.where(flat, close, open_)
    index = pd.date_range(START, periods=minutes, freq="1min", tz="UTC", name="open_time")
    frame = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index)
    return frame.drop(frame.index[rng.choice(minutes, 30, replace=False)])  # a few minutes missing entirely


def _assert_rows_equal(full: pd.DataFrame, truncated: pd.DataFrame, moment: pd.Timestamp) -> None:
    expected, actual = full.loc[moment], truncated.loc[moment]
    for column in full.columns:
        a, b = expected[column], actual[column]
        if isinstance(a, float) and np.isnan(a):
            assert np.isnan(b), column
        else:
            assert a == pytest.approx(b, rel=1e-9, abs=1e-12) if isinstance(a, float) else a == b, column


@pytest.mark.parametrize("cut_hours", [100.0, 151.0, 200.25, 263.5])
def test_symbol_features_have_no_lookahead(cut_hours):
    bars = _random_walk(1)
    full = symbol_features(bars)
    moment = START + pd.Timedelta(hours=cut_hours)
    truncated = symbol_features(bars[bars.index < moment])  # the last 1m bar closes exactly at `moment`

    assert truncated.index[-1] == moment
    _assert_rows_equal(full, truncated, moment)


def test_fair_value_features_have_no_lookahead():
    data = {"USDTTMN": _random_walk(2, start_price=270000), "BTCTMN": _random_walk(3), "BTCUSDT": _random_walk(4)}
    full = {symbol: symbol_features(bars) for symbol, bars in data.items()}
    add_fair_value(full)
    moment = START + pd.Timedelta(hours=180)
    truncated = {symbol: symbol_features(bars[bars.index < moment]) for symbol, bars in data.items()}
    add_fair_value(truncated)

    _assert_rows_equal(full["BTCTMN"], truncated["BTCTMN"], moment)
    assert "fv_z" in full["BTCTMN"] and np.isfinite(full["BTCTMN"].loc[moment, "fv_z"])


def test_higher_timeframe_value_is_the_last_closed_bar():
    bars = _random_walk(5)
    features = symbol_features(bars)
    hourly = resample(fill_gaps(bars), 60)
    moment = START + pd.Timedelta(hours=50, minutes=55)  # 50:55: the 50:00 hour has not closed yet
    last_closed = hourly[hourly["close_time"] <= moment].iloc[-1]
    assert last_closed["close_time"] == START + pd.Timedelta(hours=50)
    assert features.loc[moment, "close_1h"] == last_closed["close"]


def test_resample_keeps_only_complete_buckets_and_fills_missing_minutes():
    bars = _random_walk(6, minutes=600)  # 30 of 600 minutes missing
    filled = fill_gaps(bars)
    five = resample(filled, 5)
    assert all(five["close_time"] - five.index == pd.Timedelta(minutes=5))
    assert len(filled) == (bars.index[-1] - bars.index[0]) / pd.Timedelta(minutes=1) + 1
    in_kept = filled["gap"][filled.index.floor("5min").isin(five.index)]
    assert five["gap_minutes"].sum() == in_kept.sum() > 0
