"""Bar loading and resampling. Pure: reads Parquet, no Django, no network.

Time convention: every bar has `open_time` (start) and `close_time` (end). A
decision made at time T may use only bars with close_time <= T. The backtest
clock ticks on 5m close times.

Prices are float64 here. That is exact for every price on these markets: TMN
prices are integers below 2**53, and other prices have at most 8 decimals, so
`Decimal(repr(price))` gives back the original value. Money and quantities are
handled as Decimal in the portfolio (backtest/portfolio.py).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

OHLCV = ["open", "high", "low", "close", "volume"]


def load_1m(data_dir: Path, symbol: str) -> pd.DataFrame:
    """1m bars indexed by open_time (UTC), float64 OHLCV, sorted, without duplicates."""
    table = pq.read_table(data_dir / "candles" / "1m" / f"{symbol}.parquet")
    columns = {"open_time": table["open_time"].to_pandas()}
    for name in OHLCV:
        columns[name] = pc.cast(table[name], "float64").to_numpy()
    frame = pd.DataFrame(columns).drop_duplicates("open_time").set_index("open_time").sort_index()
    frame.index = frame.index.tz_convert("UTC")
    return frame


def fill_gaps(bars_1m: pd.DataFrame) -> pd.DataFrame:
    """Reindex to every minute. Missing minutes (exchange gaps) become flat zero-volume bars at the last close.

    The server already sends zero-volume bars for minutes without trades; this only
    covers minutes the server did not send at all, so resampled buckets stay complete.
    """
    if bars_1m.empty:
        return bars_1m
    index = pd.date_range(bars_1m.index[0], bars_1m.index[-1], freq="1min", tz="UTC")
    filled = bars_1m.reindex(index)
    missing = filled["close"].isna()
    close = filled["close"].ffill()
    for name in ("open", "high", "low"):
        filled[name] = filled[name].where(~missing, close)
    filled["close"] = close
    filled["volume"] = filled["volume"].fillna(0.0)
    filled["gap"] = missing
    filled.index.name = "open_time"
    return filled


def resample(bars_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate complete buckets only. Index = bucket open_time; adds close_time and gap_minutes."""
    rule = f"{minutes}min"
    grouped = bars_1m.resample(rule, label="left", closed="left")
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
            "count": grouped["close"].count(),
        }
    )
    if "gap" in bars_1m:
        out["gap_minutes"] = grouped["gap"].sum().astype(int)
    out = out[out["count"] == minutes].drop(columns="count")  # incomplete buckets (start/end of data) are dropped
    out["close_time"] = out.index + pd.Timedelta(minutes=minutes)
    return out


def quote_volume(bars: pd.DataFrame) -> pd.Series:
    """Approximate traded value in the quote asset (volume × typical price)."""
    return bars["volume"] * (bars["high"] + bars["low"] + bars["close"]) / 3.0


def as_of(higher: pd.DataFrame, clock: pd.DatetimeIndex, columns: list[str], suffix: str) -> pd.DataFrame:
    """Values of the latest *closed* higher-timeframe bar at each clock time (close_time <= clock)."""
    source = higher[["close_time", *columns]].sort_values("close_time")
    target = pd.DataFrame({"clock": clock})
    merged = pd.merge_asof(target, source, left_on="clock", right_on="close_time", direction="backward")
    merged = merged.drop(columns=["close_time"]).set_index("clock")
    merged.columns = [f"{name}{suffix}" for name in merged.columns]
    return merged


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close).diff()
