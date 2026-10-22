"""Loads the [universe] and [history] sections of config/trading.toml.

Decimal values are written as strings in TOML so they never pass through a float.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .universe import UniverseConfig


@dataclass(frozen=True)
class RecorderConfig:
    out_dir: Path  # relative paths are resolved against the project root by the command
    depth_interval_seconds: int
    depth_levels: int
    trades_interval_seconds: int
    tickers_interval_seconds: int
    flush_interval_seconds: int
    max_requests_per_second: float


@dataclass(frozen=True)
class MarketConfig:
    universe: UniverseConfig
    chunk_bars: int  # max bars per udf/history request
    history_requests_per_second: float
    recorder: RecorderConfig


def load_market_config(path: Path) -> MarketConfig:
    with path.open("rb") as config_file:
        config = tomllib.load(config_file)
    universe = config["universe"]
    history = config["history"]
    return MarketConfig(
        universe=UniverseConfig(
            quote_asset=str(universe["quote_asset"]),
            min_quote_volume_24h=Decimal(str(universe["min_quote_volume_24h"])),
            max_spread_fraction=Decimal(str(universe["max_spread_fraction"])),
        ),
        chunk_bars=int(history["chunk_bars"]),
        history_requests_per_second=float(history["requests_per_second"]),
        recorder=_recorder_config(config["recorder"]),
    )


def _recorder_config(section: dict) -> RecorderConfig:
    recorder = RecorderConfig(
        out_dir=Path(section["out_dir"]),
        depth_interval_seconds=int(section["depth_interval_seconds"]),
        depth_levels=int(section["depth_levels"]),
        trades_interval_seconds=int(section["trades_interval_seconds"]),
        tickers_interval_seconds=int(section["tickers_interval_seconds"]),
        flush_interval_seconds=int(section["flush_interval_seconds"]),
        max_requests_per_second=float(section["max_requests_per_second"]),
    )
    intervals = (
        recorder.depth_interval_seconds,
        recorder.trades_interval_seconds,
        recorder.tickers_interval_seconds,
        recorder.flush_interval_seconds,
    )
    if min(intervals) <= 0 or recorder.depth_levels <= 0 or recorder.max_requests_per_second <= 0:
        raise ValueError("recorder intervals, depth_levels and max_requests_per_second must be positive")
    return recorder
