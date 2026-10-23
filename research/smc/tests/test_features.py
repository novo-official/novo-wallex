import numpy as np
import pandas as pd

from research.smc.features import SMCParams, smc_frame


def _bars(closes, spread=0.5):
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    index = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + spread,
            "low": np.minimum(opens, closes) - spread,
            "close": closes,
        },
        index=index,
    )


def _random(n=600, seed=1):
    rng = np.random.default_rng(seed)
    return _bars(100 + np.cumsum(rng.normal(0, 1, n)))


def test_no_lookahead_values_never_change_when_later_bars_arrive():
    bars = _random()
    full = smc_frame(bars)
    for cut in (150, 333, 512):
        part = smc_frame(bars.iloc[:cut])
        pd.testing.assert_frame_equal(part, full.iloc[:cut], check_dtype=False)


def test_a_swing_high_is_known_only_n_bars_later():
    closes = [10, 11, 12, 15, 12, 11, 10, 10, 10]
    frame = smc_frame(_bars(closes, spread=0.1), SMCParams(pivot_n=3))
    assert np.isnan(frame["swing_high"].iloc[5])  # pivot at bar 3, not confirmed yet
    assert frame["swing_high"].iloc[6] == 15.1  # confirmed at bar 3 + 3


def test_fvg_bos_and_sweep_on_a_constructed_path():
    # down to a swing low, sweep under it and close back, then a big up move through the swing high
    closes = [20, 19, 18, 17, 16, 15, 16, 17, 18, 17, 16, 15.5, 16, 17, 16.5, 14.8, 16.2, 16.4, 21, 23, 23.5, 23, 22]
    frame = smc_frame(_bars(closes, spread=0.1), SMCParams(pivot_n=2, atr_period=3, displacement_body_atr=1.0))
    assert frame["sweep_bull"].any()
    assert frame["bos_up"].any() and frame["displacement_up"].any() and frame["fvg_new"].any()
    up = int(np.flatnonzero(frame["bos_up"].to_numpy())[0])
    sweep = int(np.flatnonzero(frame["sweep_bull"].to_numpy())[0])
    assert sweep < up
