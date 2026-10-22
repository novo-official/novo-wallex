"""Triple-barrier labels aligned with the chosen exit rules.

For a decision at bar t: entry at the maker limit close_t × (1 − offset); then the first of
  * stop  at entry × (1 − stop)        -> taker exit with slippage (stop first if both in one bar),
  * target at entry × (1 + take_profit) -> maker exit, only if a bar trades through it,
  * time  after `horizon` bars          -> taker exit at the close.
The label is 1 when the fee-adjusted return is positive. `end` is the index of the bar that
resolved the label; training samples whose `end` reaches into a test block are purged.

The entry is assumed filled (the real backtest may not fill it); that keeps labels about the
direction after a decision, while fill risk stays in the backtest.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


@dataclass(frozen=True)
class BarrierSpec:
    stop: float = 0.02
    take_profit: float = 0.06
    horizon: int = 864  # 5m bars (3 days) = max_hold_bars of the chosen params
    entry_offset: float = 0.0005
    maker_fee: float = 0.0025
    taker_fee: float = 0.0035


def triple_barrier(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    indices: np.ndarray,
    spec: BarrierSpec,
    slippage: float,
    chunk: int = 4000,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Net returns, labels and resolution indices for the decision bars `indices`.

    Decisions without `horizon` future bars get NaN returns and end = -1 (not usable).
    """
    n = len(close)
    horizon = spec.horizon
    net = np.full(len(indices), np.nan)
    end = np.full(len(indices), -1, dtype=np.int64)
    if n <= horizon:
        return net, np.zeros(len(indices), dtype=np.int8), end
    future_low = sliding_window_view(low[1:], horizon)  # row j: bars j+1 .. j+horizon
    future_high = sliding_window_view(high[1:], horizon)
    usable = indices[indices + horizon < n]
    position = {int(index): k for k, index in enumerate(indices)}
    for start in range(0, len(usable), chunk):
        batch = usable[start : start + chunk]
        entry = close[batch] * (1 - spec.entry_offset)
        stop = entry * (1 - spec.stop)
        target = entry * (1 + spec.take_profit)
        lows, highs = future_low[batch], future_high[batch]
        stop_hit = lows <= stop[:, None]
        target_hit = highs > target[:, None]
        first_stop = np.where(stop_hit.any(axis=1), stop_hit.argmax(axis=1), horizon)
        first_target = np.where(target_hit.any(axis=1), target_hit.argmax(axis=1), horizon)
        stopped = (first_stop <= first_target) & (first_stop < horizon)  # stop wins ties
        targeted = ~stopped & (first_target < horizon)
        exit_value = np.where(
            stopped,
            stop * (1 - slippage) * (1 - spec.taker_fee),
            np.where(
                targeted,
                target * (1 - spec.maker_fee),
                close[batch + horizon] * (1 - slippage) * (1 - spec.taker_fee),
            ),
        )
        returns = exit_value / (entry * (1 + spec.maker_fee)) - 1
        resolved = batch + 1 + np.where(stopped, first_stop, np.where(targeted, first_target, horizon - 1))
        for k, index in enumerate(batch):
            slot = position[int(index)]
            net[slot] = returns[k]
            end[slot] = resolved[k]
    labels = (np.nan_to_num(net, nan=-1.0) > 0).astype(np.int8)
    return net, labels, end
