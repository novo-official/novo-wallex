"""Live use of a saved per-bar model: E[net] for the latest closed bar of each traded symbol.

The engine (P5) recomputes the causal features on the latest candles, then calls `predict_latest`.
The same feature code runs in backtest, shadow and live (ml/features.py on strategy/features.py).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .dataset import ml_frames
from .model import CalibratedModel, load


class LivePredictor:
    def __init__(self, model_dir: Path) -> None:
        self.model: CalibratedModel = load(model_dir)
        self.model_id: str = self.model.meta["model_id"]

    def predict_frames(self, frames: dict[str, pd.DataFrame], symbols: list[str]) -> dict[str, pd.Series]:
        """E[net] for every row of the given ML feature frames."""
        return {symbol: pd.Series(self.model.predict(frames[symbol]), index=frames[symbol].index) for symbol in symbols}

    def predict_latest(self, features: dict[str, pd.DataFrame], symbols: list[str], breadth: list[str]) -> dict:
        frames = ml_frames(features, symbols, breadth)
        latest = {symbol: frame.tail(1) for symbol, frame in frames.items()}
        return {symbol: float(self.model.predict(frame)[0]) for symbol, frame in latest.items()}
