"""Training samples: rule-regime bars of the training symbols with triple-barrier labels."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .features import build_ml_features, uptrend_1h
from .labels import BarrierSpec, triple_barrier
from .model import Samples


def ml_frames(features: dict[str, pd.DataFrame], symbols: list[str], breadth_symbols: list[str]) -> dict:
    return {symbol: build_ml_features(features, symbol, breadth_symbols) for symbol in symbols if symbol in features}


def build_samples(
    features: dict[str, pd.DataFrame],
    frames: dict[str, pd.DataFrame],
    symbols: list[str],
    barrier: BarrierSpec,
    slippage: dict[str, float],
    sample_every: int,
    warmup_bars: int,
) -> Samples:
    """One sample every `sample_every` bars while the 1h uptrend regime is on (the population the rules trade)."""
    parts = []
    for symbol in symbols:
        frame = features[symbol]
        regime = uptrend_1h(frame).to_numpy()
        candidate = np.flatnonzero(regime)
        candidate = candidate[(candidate >= warmup_bars) & (candidate % sample_every == 0)]
        net, labels, end = triple_barrier(
            frame["high"].to_numpy(float),
            frame["low"].to_numpy(float),
            frame["close"].to_numpy(float),
            candidate,
            barrier,
            slippage.get(symbol, 0.002),
        )
        keep = end >= 0
        index = frame.index
        parts.append(
            pd.DataFrame(
                {
                    "t": index[candidate[keep]],
                    "t_end": index[end[keep]],
                    "y": labels[keep],
                    "net": net[keep],
                    "symbol": symbol,
                }
            ).join(frames[symbol].iloc[candidate[keep]].reset_index(drop=True))
        )
    table = pd.concat(parts, ignore_index=True).sort_values("t").reset_index(drop=True)
    feature_columns = list(frames[symbols[0]].columns)
    return Samples(
        X=table[feature_columns],
        y=table["y"].to_numpy(np.int8),
        net=table["net"].to_numpy(float),
        t=pd.DatetimeIndex(table["t"]),
        t_end=pd.DatetimeIndex(table["t_end"]),
        symbol=table["symbol"].to_numpy(),
    )
