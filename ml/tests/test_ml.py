import numpy as np
import pandas as pd
import pytest

from ml.dataset import build_samples, ml_frames
from ml.features import FEATURES, build_ml_features
from ml.labels import BarrierSpec, triple_barrier
from ml.model import Samples, WalkForwardSpec, fit, load, save, walk_forward
from strategy.base import Context, FeatureFrame, Intent, PortfolioView
from strategy.composite import CompositeStrategy, MLFilterParams
from strategy.features import symbol_features
from strategy.tests.test_features import START, _random_walk

SPEC = BarrierSpec(stop=0.02, take_profit=0.04, horizon=5, entry_offset=0.0, maker_fee=0.0, taker_fee=0.0)


def _bars(high, low, close):
    return np.asarray(high, float), np.asarray(low, float), np.asarray(close, float)


def test_take_profit_needs_trade_through_and_stop_wins_ties():
    high, low, close = _bars([100, 104, 105, 100, 100, 100, 100], [100, 99, 99, 99, 99, 99, 99], [100] * 7)
    net, label, end = triple_barrier(high, low, close, np.array([0]), SPEC, slippage=0.0)
    assert label[0] == 1 and net[0] == pytest.approx(0.04) and end[0] == 2  # 104 touches, 105 trades through

    high, low, close = _bars([100, 105, 100, 100, 100, 100, 100], [100, 97, 99, 99, 99, 99, 99], [100] * 7)
    net, label, end = triple_barrier(high, low, close, np.array([0]), SPEC, slippage=0.0)
    assert label[0] == 0 and net[0] == pytest.approx(-0.02) and end[0] == 1  # both in bar 1: stop first


def test_time_exit_uses_the_close_after_the_horizon_and_fees_count():
    spec = BarrierSpec(stop=0.05, take_profit=0.05, horizon=3, entry_offset=0.0, maker_fee=0.0025, taker_fee=0.0035)
    high, low, close = _bars([100, 101, 101, 101, 101], [99.5] * 5, [100, 100.5, 100.5, 100.4, 100])
    net, label, end = triple_barrier(high, low, close, np.array([0]), spec, slippage=0.0)
    assert end[0] == 3
    assert net[0] == pytest.approx(100.4 * (1 - 0.0035) / (100 * 1.0025) - 1)
    assert label[0] == 0  # +0.4% move, but fees make it a loss


def test_decisions_without_a_full_horizon_are_unusable():
    high, low, close = _bars([100] * 6, [100] * 6, [100] * 6)
    net, _, end = triple_barrier(high, low, close, np.array([0, 3]), SPEC, slippage=0.0)
    assert end[1] == -1 and np.isnan(net[1]) and end[0] == 5


def _features():
    minutes = 20 * 24 * 60  # the 1h EMA200 needs ~8.3 days before the first sample
    raw = {
        "USDTTMN": _random_walk(11, minutes=minutes, start_price=270000),
        "BTCTMN": _random_walk(12, minutes=minutes),
        "ETHTMN": _random_walk(13, minutes=minutes),
    }
    return {symbol: symbol_features(bars) for symbol, bars in raw.items()}, raw


def test_ml_features_have_no_lookahead():
    full, raw = _features()
    moment = START + pd.Timedelta(hours=200)
    truncated = {symbol: symbol_features(bars[bars.index < moment]) for symbol, bars in raw.items()}
    a = build_ml_features(full, "BTCTMN", list(full)).loc[moment]
    b = build_ml_features(truncated, "BTCTMN", list(truncated)).loc[moment]
    pd.testing.assert_series_equal(a, b, check_names=False, rtol=1e-9)
    assert list(a.index) == FEATURES


def _samples():
    features, _ = _features()
    frames = ml_frames(features, list(features), list(features))
    spec = BarrierSpec(horizon=48)
    return features, frames, build_samples(features, frames, list(features), spec, {}, 3, 288 * 3), spec


def test_walk_forward_trains_only_on_labels_resolved_before_each_block():
    features, frames, samples, barrier = _samples()
    assert len(samples.y) > 200
    spec = WalkForwardSpec(min_train_days=4, block_days=2, calibration_share=0.2)
    edges, blocks = walk_forward(samples, {"BTCTMN": frames["BTCTMN"]}, spec, barrier.horizon * pd.Timedelta(minutes=5))
    first_block = blocks["block_start"].iloc[0]
    assert edges["BTCTMN"][edges["BTCTMN"].index < first_block].isna().all()  # no model, no value
    assert edges["BTCTMN"][edges["BTCTMN"].index >= first_block].notna().any()
    for _, row in blocks.iterrows():
        assert row["train_rows"] == int((samples.t_end < row["block_start"]).sum())


def test_saved_model_predicts_the_same(tmp_path):
    _, _, samples, barrier = _samples()
    model = fit(samples, barrier.horizon * pd.Timedelta(minutes=5), 0.2)
    model_id = save(model, tmp_path, {"label": "test"})
    again = load(tmp_path)
    np.testing.assert_allclose(model.predict(samples.X.head(200)), again.predict(samples.X.head(200)))
    assert again.meta["model_id"] == model_id and len(model_id) == 16


def _ctx(edge: float) -> Context:
    clock = pd.date_range(START, periods=2, freq="5min", tz="UTC")
    frame = FeatureFrame(pd.DataFrame({"close": [100.0, 100.0], "ml_edge": [edge, edge]}, index=clock))
    ctx = Context(clock, {"AAATMN": frame}, ["AAATMN"])
    ctx.index = 1
    return ctx


class _Always:
    name = "trend"

    def entry(self, ctx, symbol):
        return Intent(kind="ENTER", symbol=symbol, module="trend", reason="r", limit_price=100, stop_price=98)

    def manage(self, ctx, symbol, position):
        return []


VIEW = PortfolioView(equity=0, cash=0, positions={}, pending_entries=frozenset(), entries_allowed=True)


@pytest.mark.parametrize(
    ("edge", "mode", "expected"),
    [
        (0.10, "filter", 1.0),
        (0.00, "filter", None),
        (float("nan"), "filter", None),
        (0.00, "tilt", 0.55),
        (float("nan"), "tilt", None),
    ],
)
def test_ml_filter_blocks_tilts_and_fails_closed(edge, mode, expected):
    strategy = CompositeStrategy([_Always()], ["AAATMN"], ml=MLFilterParams(enabled=True, threshold=0.05, mode=mode))
    intents = strategy.on_cycle(_ctx(edge), VIEW)
    if expected is None:
        assert intents == []
    else:
        (intent,) = intents
        assert intent.size_fraction == pytest.approx(expected)
        assert "ML ml_edge" in intent.reason and "ml_edge" in intent.signals


def test_samples_are_time_ordered_with_resolution_after_decision():
    _, _, samples, _ = _samples()
    assert isinstance(samples, Samples)
    assert (np.diff(samples.t.asi8) >= 0).all()
    assert (samples.t_end > samples.t).all()


def test_module_size_survives_the_composite_when_fair_value_is_off():
    # Regression: the fair-value step used to reset every module's size_fraction to 1.0.
    class Small(_Always):
        def entry(self, ctx, symbol):
            intent = super().entry(ctx, symbol)
            from dataclasses import replace

            return replace(intent, size_fraction=0.1)

    (intent,) = CompositeStrategy([Small()], ["AAATMN"]).on_cycle(_ctx(0.0), VIEW)
    assert intent.size_fraction == pytest.approx(0.1)


def test_saved_net_model_keeps_negative_expected_returns(tmp_path):
    from sklearn.isotonic import IsotonicRegression

    from ml.model import CalibratedModel

    _, _, samples, barrier = _samples()
    model = fit(samples, barrier.horizon * pd.Timedelta(minutes=5), 0.2)
    calibrator = IsotonicRegression(out_of_bounds="clip").fit([0.0, 0.5, 1.0], [-0.006, -0.004, 0.002])
    save(CalibratedModel(model.booster, calibrator, model.features, {}), tmp_path, {})
    again = load(tmp_path)
    assert again.calibrator.predict([0.0])[0] == pytest.approx(-0.006)  # regression: not clipped to 0
