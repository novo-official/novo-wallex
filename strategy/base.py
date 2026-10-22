"""The strategy interface shared by backtest, shadow and live.

    Strategy.on_cycle(ctx, portfolio) -> list[Intent]

The strategy only *proposes*: an Intent carries the signal values and a
human-readable reason (they become the decision log). Sizing caps, exposure,
loss limits and end-of-challenge rules are applied afterwards by the risk layer
(risk/limits.py), which has the final word.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

import numpy as np
import pandas as pd

IntentKind = Literal["ENTER", "EXIT", "SET_STOP"]


@dataclass(frozen=True)
class Intent:
    kind: IntentKind
    symbol: str
    module: str
    reason: str
    signals: dict[str, float] = field(default_factory=dict)
    limit_price: float | None = None  # ENTER: maker buy limit
    stop_price: float | None = None  # ENTER, SET_STOP
    take_profit: float | None = None  # ENTER: maker sell limit
    size_fraction: float = 1.0  # ENTER: share of the maximum position the risk layer allows
    timeout_bars: int = 3  # ENTER: cancel the limit after this many 5m bars
    entry_type: str = "maker"  # ENTER: "maker" limit that must be traded through, or "taker" at the next open
    exit_type: str = "taker"  # EXIT: "taker" at the next open, or "maker" limit at the close (+offset), then taker


@dataclass(frozen=True)
class PositionView:
    symbol: str
    module: str
    entry_price: Decimal
    quantity: Decimal
    stop_price: Decimal
    take_profit: Decimal | None
    initial_risk: Decimal  # entry - initial stop, per unit
    opened_index: int
    highest_close: float


@dataclass(frozen=True)
class PortfolioView:
    equity: Decimal
    cash: Decimal
    positions: dict[str, PositionView]
    pending_entries: frozenset[str]
    entries_allowed: bool  # False after the daily loss limit, near the end, or after the kill switch


BOOL_COLUMNS = frozenset({"active", "fv_valid"})  # become object dtype after a reindex with gaps


class FeatureFrame:
    """Column arrays of one symbol on the shared 5m clock. Values are float64; NaN means not available."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.columns: dict[str, np.ndarray] = {}
        for name in frame.columns:
            values = frame[name]
            if pd.api.types.is_bool_dtype(values) or name in BOOL_COLUMNS:
                self.columns[name] = values.fillna(False).astype(bool).to_numpy()
            elif pd.api.types.is_numeric_dtype(values):
                self.columns[name] = values.to_numpy(dtype=float)

    def __getitem__(self, name: str) -> np.ndarray:
        return self.columns[name]

    def __contains__(self, name: str) -> bool:
        return name in self.columns


class Context:
    """What a strategy may see at clock index `index`: only rows <= index (closed bars)."""

    def __init__(self, clock: pd.DatetimeIndex, features: dict[str, FeatureFrame], symbols: list[str]) -> None:
        self.clock = clock
        self.features = features
        self.symbols = symbols
        self.index = 0

    @property
    def now(self) -> pd.Timestamp:
        return self.clock[self.index]

    def value(self, symbol: str, column: str, lag: int = 0) -> float:
        position = self.index - lag
        if position < 0:
            return float("nan")
        return float(self.features[symbol][column][position])

    def window(self, symbol: str, column: str, length: int) -> np.ndarray:
        """The last `length` values up to and including now."""
        start = max(0, self.index - length + 1)
        return self.features[symbol][column][start : self.index + 1]


class Strategy(Protocol):
    name: str

    def on_cycle(self, ctx: Context, portfolio: PortfolioView) -> list[Intent]: ...
