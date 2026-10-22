"""Builds the backtest's MarketData from the committed Parquet files (no DB, no network)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pandas as pd

from strategy.base import FeatureFrame
from strategy.data import load_1m
from strategy.features import add_fair_value, symbol_features

from .engine import MarketData, SymbolRules

RULES_FILE = "market_rules.json"


def load_rules(data_dir: Path) -> dict[str, SymbolRules]:
    raw = json.loads((data_dir / RULES_FILE).read_text(encoding="utf-8"))
    return {
        symbol: SymbolRules(
            amount_decimals=int(rule["amount_decimals"]),
            price_decimals=int(rule["price_decimals"]),
            min_notional=Decimal(rule["min_notional"]),
        )
        for symbol, rule in raw["markets"].items()
    }


def build_features(data_dir: Path, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Per-symbol 5m features (index = close_time) for every symbol with a candle file."""
    features: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = data_dir / "candles" / "1m" / f"{symbol}.parquet"
        if path.exists():
            features[symbol] = symbol_features(load_1m(data_dir, symbol))
    add_fair_value(features)
    return features


def build_market_data(features: dict[str, pd.DataFrame], tradable: list[str], rules: dict[str, SymbolRules]):
    """Align every symbol on one 5m clock (the intersection of their ranges)."""
    start = max(frame.index[0] for frame in features.values())
    end = min(frame.index[-1] for frame in features.values())
    clock = pd.date_range(start, end, freq="5min", tz="UTC")
    frames = {symbol: FeatureFrame(frame.reindex(clock)) for symbol, frame in features.items()}
    for symbol, frame in frames.items():  # a bar missing on the clock must not look tradable
        for name in ("open", "high", "low", "close"):
            if name in frame:
                filled = pd.Series(frame[name]).ffill().to_numpy()
                frame.columns[name] = filled
    return MarketData(clock=clock, features=frames, rules=rules, symbols=[s for s in tradable if s in frames])
