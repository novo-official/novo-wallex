"""Event study: does a signal have an edge after costs, before any portfolio effects?

Each signal event (a bar where the signal is true and the symbol is flat) is simulated
as an independent trade with the same conservative fill rules as the backtester:
  * entry "maker": buy limit at close × (1 − offset), fills only if a later bar trades below it
    within `timeout` bars; entry "taker": buy at the next bar's open + slippage, taker fee;
  * exit at a stop, a take-profit or after `max_bars`, stop first when both are in one bar,
    stops and time exits as taker fills with slippage, take-profits as maker fills.
The baseline is the same exit rule applied to every flat bar (unconditional entry): an
edge is the difference between the signal and the baseline, net of fees.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ExitRule:
    stop: float  # fraction below entry
    take_profit: float  # fraction above entry
    max_bars: int


@dataclass(frozen=True)
class StudyResult:
    events: int
    filled: int
    mean: float  # mean net return per filled trade
    median: float
    win_rate: float
    t_stat: float
    total: float  # sum of net returns (fraction of position)

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def simulate(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    signal: np.ndarray,
    rule: ExitRule,
    *,
    maker_fee: float,
    taker_fee: float,
    slippage: float,
    entry: str = "maker",
    offset: float = 0.0005,
    timeout: int = 3,
    start: int = 0,
    end: int | None = None,
) -> list[float]:
    """Net returns of non-overlapping trades triggered by `signal` in [start, end)."""
    end = len(close) if end is None else end
    returns: list[float] = []
    index = start
    while index < end - 1:
        if not signal[index]:
            index += 1
            continue
        fill_index, entry_price, entry_fee = None, 0.0, 0.0
        if entry == "maker":
            limit = close[index] * (1 - offset)
            for j in range(index + 1, min(index + 1 + timeout, end)):
                if low[j] < limit:
                    fill_index, entry_price, entry_fee = j, limit, maker_fee
                    break
        else:
            fill_index, entry_price, entry_fee = index + 1, open_[index + 1] * (1 + slippage), taker_fee
        if fill_index is None:
            index += timeout + 1
            continue
        stop, target = entry_price * (1 - rule.stop), entry_price * (1 + rule.take_profit)
        exit_price, exit_fee, exit_index = None, taker_fee, None
        last = min(fill_index + rule.max_bars, end - 1)
        for j in range(fill_index, last + 1):
            if low[j] <= stop:
                exit_price = min(stop, open_[j] if j > fill_index else stop) * (1 - slippage)
                exit_index = j
                break
            if j > fill_index and high[j] > target:
                exit_price, exit_fee, exit_index = target, maker_fee, j
                break
        if exit_price is None:
            exit_index = last
            exit_price = close[last] * (1 - slippage)
        returns.append((exit_price * (1 - exit_fee)) / (entry_price * (1 + entry_fee)) - 1)
        index = exit_index + 1
    return returns


def summarize(returns: list[float], events: int) -> StudyResult:
    values = np.asarray(returns, dtype=float)
    if values.size == 0:
        return StudyResult(events, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    std = values.std(ddof=1) if values.size > 1 else 0.0
    t_stat = float(values.mean() / (std / math.sqrt(values.size))) if std > 0 else 0.0
    return StudyResult(
        events=events,
        filled=int(values.size),
        mean=float(values.mean()),
        median=float(np.median(values)),
        win_rate=float((values > 0).mean()),
        t_stat=t_stat,
        total=float(values.sum()),
    )
