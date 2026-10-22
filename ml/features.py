"""Model inputs on the 5m clock. Built only from the causal strategy features, so they inherit
the no-lookahead guarantee (strategy/tests/test_features.py; ml/tests/test_ml.py checks the extras).

Prices never enter directly: every input is a ratio, a return, an oscillator or a flag, so one
model can be trained across symbols with very different price levels.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TEHRAN = "Asia/Tehran"

FEATURES = [
    "ret_1h",
    "ret_4h",
    "ret_1d",
    "ret_3d",
    "vol_1d",
    "atr_5m_rel",
    "atr_1h_rel",
    "dist_ema20_15m",
    "dist_ema50_1h",
    "dist_ema200_1h",
    "dist_ema50_4h",
    "ema50_slope_1h",
    "ema50_slope_4h",
    "rsi14_15m",
    "rsi14_1h",
    "rsi14_4h",
    "adx14_1h",
    "adx14_4h",
    "bb_pos_15m",
    "bb_width_1h",
    "breakout_strength",
    "is_breakout",
    "active_share_1d",
    "log_quote_volume_1d",
    "volume_surge_1h",
    "fv_z",
    "fv_premium",
    "usdt_ret_4h",
    "usdt_ret_1d",
    "usdt_dist_ema200_1h",
    "btc_ret_4h",
    "btc_ret_1d",
    "breadth_up_1h",
    "hour_sin",
    "hour_cos",
    "is_thursday",
    "is_friday",
]


def uptrend_1h(frame: pd.DataFrame) -> pd.Series:
    """The rules' regime (trend module, 1h): close > EMA200, EMA50 > EMA200, EMA50 rising."""
    return (
        (frame["close_1h"] > frame["ema200_1h"])
        & (frame["ema50_1h"] > frame["ema200_1h"])
        & (frame["ema50_slope_1h"] > 0)
    ).fillna(False)


def breakout_trigger(frame: pd.DataFrame, bars: int = 12) -> pd.Series:
    """The rules' 5m trigger: the bar traded and closed above the previous `bars` highs and the 15m EMA20."""
    return (
        (frame["volume"] > 0) & (frame["close"] > frame[f"prior_high_{bars}"]) & (frame["close"] > frame["ema20_15m"])
    ).fillna(False)


def _context(features: dict[str, pd.DataFrame], symbol: str, column: str, index: pd.Index) -> pd.Series:
    if symbol not in features:
        return pd.Series(np.nan, index=index)
    return features[symbol][column].reindex(index)


def build_ml_features(features: dict[str, pd.DataFrame], symbol: str, breadth_symbols: list[str]) -> pd.DataFrame:
    frame = features[symbol]
    close = frame["close"]
    log_close = np.log(close)
    out = pd.DataFrame(index=frame.index)
    out["ret_1h"] = frame["ret_1h"]
    out["ret_4h"] = log_close.diff(48)
    out["ret_1d"] = log_close.diff(288)
    out["ret_3d"] = log_close.diff(864)
    out["vol_1d"] = frame["vol_1d"]
    out["atr_5m_rel"] = frame["atr14_5m"] / close
    out["atr_1h_rel"] = frame["atr14_1h"] / close
    out["dist_ema20_15m"] = close / frame["ema20_15m"] - 1
    out["dist_ema50_1h"] = close / frame["ema50_1h"] - 1
    out["dist_ema200_1h"] = close / frame["ema200_1h"] - 1
    out["dist_ema50_4h"] = close / frame["ema50_4h"] - 1
    out["ema50_slope_1h"] = frame["ema50_slope_1h"]
    out["ema50_slope_4h"] = frame["ema50_4h"] / frame["ema50_4h"].shift(48) - 1
    for name in ("rsi14_15m", "rsi14_1h", "rsi14_4h", "adx14_1h", "adx14_4h"):
        out[name] = frame[name]
    band = frame["bb_high_15m"] - frame["bb_low_15m"]
    out["bb_pos_15m"] = (frame["close_15m"] - frame["bb_low_15m"]) / band.replace(0, np.nan)
    out["bb_width_1h"] = (frame["bb_high_1h"] - frame["bb_low_1h"]) / frame["bb_mid_1h"]
    out["breakout_strength"] = close / frame["prior_high_12"] - 1
    out["is_breakout"] = breakout_trigger(frame).astype(float)
    out["active_share_1d"] = frame["active_share_1d"]
    out["log_quote_volume_1d"] = np.log1p(frame["quote_volume_1d"])
    hourly_volume = frame["quote_volume"].rolling(12, min_periods=12).sum()
    out["volume_surge_1h"] = hourly_volume / (frame["quote_volume_1d"] / 24).replace(0, np.nan)
    out["fv_z"] = frame["fv_z"] if "fv_z" in frame else np.nan
    out["fv_premium"] = frame["fv_premium"] if "fv_premium" in frame else np.nan

    usdt_close = _context(features, "USDTTMN", "close", frame.index)
    out["usdt_ret_4h"] = np.log(usdt_close).diff(48)
    out["usdt_ret_1d"] = np.log(usdt_close).diff(288)
    out["usdt_dist_ema200_1h"] = usdt_close / _context(features, "USDTTMN", "ema200_1h", frame.index) - 1
    btc_close = _context(features, "BTCTMN", "close", frame.index)
    out["btc_ret_4h"] = np.log(btc_close).diff(48)
    out["btc_ret_1d"] = np.log(btc_close).diff(288)
    up = [uptrend_1h(features[s]).reindex(frame.index).astype(float) for s in breadth_symbols if s in features]
    out["breadth_up_1h"] = pd.concat(up, axis=1).mean(axis=1) if up else np.nan

    local = frame.index.tz_convert(TEHRAN)
    # The decision is made at the bar close; use the hour of that moment.
    out["hour_sin"] = np.sin(2 * np.pi * local.hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * local.hour / 24)
    out["is_thursday"] = (local.dayofweek == 3).astype(float)
    out["is_friday"] = (local.dayofweek == 4).astype(float)
    return out[FEATURES].replace([np.inf, -np.inf], np.nan)
