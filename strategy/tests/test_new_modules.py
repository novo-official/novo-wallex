import numpy as np
import pandas as pd
import pytest

from backtest.dataset import detrend
from strategy.base import Context, FeatureFrame
from strategy.modules.ml_signal import MLSignalModule, MLSignalParams
from strategy.modules.usdt_allocation import USDTAllocationModule, USDTAllocationParams

CLOCK = pd.date_range("2026-10-01T00:00:00Z", periods=3, freq="5min", tz="UTC")


def _ctx(symbol: str, **columns) -> Context:
    frame = pd.DataFrame({"close": 100.0, "volume": 1.0, **columns}, index=CLOCK)
    ctx = Context(CLOCK, {symbol: FeatureFrame(frame)}, [symbol])
    ctx.index = 2
    return ctx


def test_ml_signal_enters_above_threshold_with_confidence_size_and_exits_on_time():
    module = MLSignalModule(MLSignalParams(column="ml_net_h48", threshold=0.0, full_size_margin=0.004))
    assert module.entry(_ctx("BTCTMN", ml_net_h48=-0.001), "BTCTMN") is None
    assert module.entry(_ctx("BTCTMN", ml_net_h48=float("nan")), "BTCTMN") is None
    intent = module.entry(_ctx("BTCTMN", ml_net_h48=0.002), "BTCTMN")
    assert intent.size_fraction == pytest.approx(0.55 + 0.45 * 0.5)
    assert intent.stop_price == pytest.approx(intent.limit_price * (1 - 0.015))

    class Held:
        opened_index = -46

    (exit_intent,) = module.manage(_ctx("BTCTMN", ml_net_h48=0.0), "BTCTMN", Held())
    assert exit_intent.kind == "EXIT"


def test_usdt_allocation_follows_the_regime_and_caps_the_stop():
    up = {"close_4h": 110.0, "ema20_4h": 105.0, "ema50_4h": 100.0, "atr14_1h": 2.0}
    module = USDTAllocationModule(USDTAllocationParams(regime="4h", stop_atr=3.0))
    intent = module.entry(_ctx("USDTTMN", **up), "USDTTMN")
    assert intent is not None and (intent.limit_price - intent.stop_price) / intent.limit_price == pytest.approx(0.02)
    down = {**up, "close_4h": 99.0}
    assert module.entry(_ctx("USDTTMN", **down), "USDTTMN") is None
    (exit_intent,) = module.manage(_ctx("USDTTMN", **down), "USDTTMN", object())
    assert "down" in exit_intent.reason
    assert module.entry(_ctx("USDTTMN", **up), "USDTTMN") is None  # cooldown after the exit
    assert module.entry(_ctx("BTCTMN", **up), "BTCTMN") is None  # only its own symbol


def test_detrend_removes_the_average_drift():
    index = pd.date_range("2026-01-01", periods=1000, freq="1min", tz="UTC")
    close = pd.Series(100 * 1.0001 ** np.arange(1000), index=index, dtype=float)
    bars = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0})
    flat = detrend(bars)
    assert flat["close"].iloc[-1] == pytest.approx(flat["close"].iloc[0])
    assert (flat["volume"] == 1.0).all()
