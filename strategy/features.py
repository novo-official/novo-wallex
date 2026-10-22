"""Causal features on the 5m decision clock.

Every value at clock time T uses only bars with close_time <= T: rolling windows
look backwards on closed bars, and higher timeframes (15m, 1h, 4h) enter through
`as_of`, which takes the latest bar that has closed by T. The lookahead test
(strategy/tests/test_features.py) recomputes features on data truncated at T and
checks they equal the full-history values at T.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import as_of, fill_gaps, quote_volume, resample

BARS_PER_DAY_5M = 288


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def atr(bars: pd.DataFrame, period: int) -> pd.Series:
    previous_close = bars["close"].shift(1)
    true_range = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - previous_close).abs(), (bars["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    return wilder(true_range, period)


def rsi(close: pd.Series, period: int) -> pd.Series:
    change = close.diff()
    gain = wilder(change.clip(lower=0.0), period)
    loss = wilder((-change).clip(lower=0.0), period)
    relative = gain / loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + relative)).fillna(100.0).where(gain.notna())


def adx(bars: pd.DataFrame, period: int) -> pd.Series:
    up = bars["high"].diff()
    down = -bars["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    true_range = atr(bars, period)
    plus_di = 100 * wilder(plus_dm, period) / true_range
    minus_di = 100 * wilder(minus_dm, period) / true_range
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return wilder(dx, period)


def timeframe_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Indicators computed on one timeframe's own closed bars."""
    close = bars["close"]
    mid = close.rolling(20, min_periods=20).mean()
    std = close.rolling(20, min_periods=20).std(ddof=0)
    return pd.DataFrame(
        {
            "close_time": bars["close_time"],
            "close": close,
            "ema20": ema(close, 20),
            "ema50": ema(close, 50),
            "ema200": ema(close, 200),
            "rsi14": rsi(close, 14),
            "atr14": atr(bars, 14),
            "adx14": adx(bars, 14),
            "bb_mid": mid,
            "bb_low": mid - 2 * std,
            "bb_high": mid + 2 * std,
        },
        index=bars.index,
    )


def symbol_features(bars_1m: pd.DataFrame) -> pd.DataFrame:
    """All single-symbol features on the 5m clock (index = 5m close_time)."""
    filled = fill_gaps(bars_1m)
    bars_5m = resample(filled, 5)
    clock = pd.DatetimeIndex(bars_5m["close_time"])
    base = bars_5m.set_index("close_time")
    out = pd.DataFrame(index=clock)
    for name in ("open", "high", "low", "close", "volume", "gap_minutes"):
        out[name] = base[name].to_numpy()
    out["open_time"] = bars_5m.index
    out["quote_volume"] = quote_volume(base).to_numpy()
    out["active"] = (base["volume"] > 0).to_numpy()
    out["atr14_5m"] = atr(base, 14).to_numpy()
    # Breakout reference: highest high of the previous 12 bars (one hour), excluding the current bar.
    for lookback in (6, 12, 24):
        out[f"prior_high_{lookback}"] = base["high"].shift(1).rolling(lookback, min_periods=lookback).max().to_numpy()
    out["active_share_1d"] = out["active"].astype(float).rolling(BARS_PER_DAY_5M, min_periods=48).mean()
    out["quote_volume_1d"] = out["quote_volume"].rolling(BARS_PER_DAY_5M, min_periods=48).sum()
    out["ret_1h"] = np.log(out["close"]).diff(12)
    out["vol_1d"] = np.log(out["close"]).diff().rolling(BARS_PER_DAY_5M, min_periods=48).std()

    for minutes, suffix in ((15, "_15m"), (60, "_1h"), (240, "_4h")):
        higher = timeframe_features(resample(filled, minutes))
        out = out.join(as_of(higher, clock, [c for c in higher.columns if c != "close_time"], suffix))
    out["ema50_slope_1h"] = out["ema50_1h"] / out["ema50_1h"].shift(12) - 1.0  # change over the last hour
    return out


def add_fair_value(features: dict[str, pd.DataFrame], window: int = BARS_PER_DAY_5M) -> None:
    """Premium of each TMN market over its USDT pair converted at USDTTMN, and its rolling z-score.

    premium = close_TMN / (close_USDT_pair × close_USDTTMN) − 1. A stale USDT pair (few
    active bars) makes the premium meaningless, so `fv_valid` requires both legs to have
    traded in at least half of the last day's 5m bars.
    """
    usdt_tmn = features.get("USDTTMN")
    if usdt_tmn is None:
        return
    for symbol, frame in features.items():
        if not symbol.endswith("TMN") or symbol == "USDTTMN":
            continue
        pair = features.get(symbol.removesuffix("TMN") + "USDT")
        if pair is None:
            continue
        fair = pair["close"].reindex(frame.index) * usdt_tmn["close"].reindex(frame.index)
        premium = frame["close"] / fair - 1.0
        mean = premium.rolling(window, min_periods=window // 2).mean()
        std = premium.rolling(window, min_periods=window // 2).std()
        frame["fv_premium"] = premium
        frame["fv_z"] = (premium - mean) / std.replace(0.0, np.nan)
        pair_active = pair["active_share_1d"].reindex(frame.index)
        frame["fv_valid"] = (pair_active >= 0.5) & (frame["active_share_1d"] >= 0.5) & frame["fv_z"].notna()
