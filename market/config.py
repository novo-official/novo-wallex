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
class MarketConfig:
    universe: UniverseConfig
    chunk_bars: int  # max bars per udf/history request


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
    )
