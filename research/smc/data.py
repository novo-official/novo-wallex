"""SMC columns on the strategy's 5m clock: 15m setup/structure (suffix _smc15) and 1h structure (_smc1h), joined
as of the last *closed* higher-timeframe bar. Builds on the same features and cached walk-forward h24 column as
the backtests (ml/p3_experiments.build_market), without changing them."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from backtest.dataset import build_features, build_market_data, load_rules
from ml.dataset import ml_frames
from ml.p3_experiments import CACHE, TARGETS, _cache_key
from strategy.data import load_1m
from strategy.smc import SMCParams, smc_on_clock

H24 = next(t for t in TARGETS if t.name == "h24")
ML_SYMBOLS = ["USDTTMN", "BTCTMN", "ETHTMN"]


def smc_columns(data_dir: Path, symbol: str, clock: pd.DatetimeIndex, params: SMCParams, tag: str = "") -> pd.DataFrame:
    return smc_on_clock(load_1m(data_dir, symbol), clock, params, tag)


def smc_features(config, data_dir: Path, symbols: list[str], variants: dict[str, SMCParams]) -> tuple[dict, dict]:
    """(features, frames) with SMC columns on `symbols` for every {tag: params} and the cached walk-forward
    ml_net_h24 column."""
    universe = list(config.tradable) + [s.removesuffix("TMN") + "USDT" for s in config.tradable if s != "USDTTMN"]
    features = build_features(data_dir, universe)
    frames = ml_frames(features, list(config.tradable), list(config.tradable))
    cached = pd.read_parquet(CACHE / f"{H24.name}-{_cache_key(data_dir, H24, ML_SYMBOLS, False)}.parquet")
    for symbol in features:
        features[symbol]["ml_net_h24"] = cached[symbol] if symbol in cached else np.nan
    for tag, params in variants.items():
        for symbol in symbols:
            extra = smc_columns(data_dir, symbol, pd.DatetimeIndex(features[symbol].index), params, tag)
            for column in extra.columns:
                features[symbol][column] = extra[column].to_numpy()
    return features, frames


def market(config, features: dict, data_dir: Path):
    return build_market_data(features, config.tradable, load_rules(data_dir))
