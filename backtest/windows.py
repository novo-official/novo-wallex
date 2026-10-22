"""Rolling 3-day window study: independent runs from 5M TMN starting every 12 h.

The challenge is one 3-day run, so the distribution over many 3-day windows is the
evidence that matters: share of positive windows, median/p10/p90 return and worst
drawdown. Parameters are selected on the first 70% of windows and reported on the
last 30% (holdout), with the fee-stress case alongside.

Runs are independent, so they are spread over processes. The fork start method
shares the precomputed features with the workers without copying them.
"""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from risk.limits import RiskLimits

from .costs import CostModel
from .engine import Backtester, MarketData
from .metrics import compute_metrics


@dataclass(frozen=True)
class WindowSpec:
    window_days: float = 3.0
    step_hours: float = 12.0
    warmup_days: float = 10.0  # the 1h EMA200 needs ~8.3 days of history


def window_starts(clock: pd.DatetimeIndex, spec: WindowSpec) -> list[pd.Timestamp]:
    first = clock[0] + pd.Timedelta(days=spec.warmup_days)
    last = clock[-1] - pd.Timedelta(days=spec.window_days)
    starts = []
    moment = first.ceil("12h")
    while moment <= last:
        starts.append(moment)
        moment += pd.Timedelta(hours=spec.step_hours)
    return starts


def split_selection_holdout(starts: list[pd.Timestamp], spec: WindowSpec, selection_share: float = 0.7):
    """First 70% of windows for selection; holdout windows start after the last selection window ends."""
    cut = int(len(starts) * selection_share)
    selection = starts[:cut]
    if not selection:
        return [], starts
    boundary = selection[-1] + pd.Timedelta(days=spec.window_days)
    return selection, [start for start in starts if start >= boundary]


def limits_for(limits: RiskLimits, params: dict) -> RiskLimits:
    """Apply a variant's risk overrides, e.g. {"risk": {"position_caps": {"USDTTMN": 0.4}, "min_order_tmn": 50000}}."""
    risk = params.get("risk", {})
    if not risk:
        return limits
    from dataclasses import replace
    from decimal import Decimal

    changes = {}
    if risk.get("position_caps"):
        caps = risk["position_caps"]
        changes["position_caps"] = tuple((symbol, Decimal(str(cap))) for symbol, cap in sorted(caps.items()))
    if "min_order_tmn" in risk:
        changes["min_order_tmn"] = Decimal(str(risk["min_order_tmn"]))
    return replace(limits, **changes)


# Shared state for forked workers.
_STATE: dict = {}


def _run_one(task: tuple) -> dict:
    params_key, start = task
    data: MarketData = _STATE["data"]
    factory: Callable = _STATE["factory"]
    costs: CostModel = _STATE["costs"][params_key[1]]
    limits: RiskLimits = _STATE["limits"]
    spec: WindowSpec = _STATE["spec"]
    params = _STATE["params"][params_key[0]]
    limits = limits_for(limits, params)
    strategy = factory(params, data.symbols)
    end = start + pd.Timedelta(days=spec.window_days)
    result = Backtester(data, strategy, costs, limits, record_decisions=False).run(start, end)
    metrics = compute_metrics(result)
    return {
        "params_id": params_key[0],
        "cost_case": params_key[1],
        "start": start.isoformat(),
        "return": metrics["return"],
        "max_drawdown": metrics["max_drawdown"],
        "trades": metrics["trades"],
        "win_rate": metrics["win_rate"],
        "fees_share": metrics["fees_share_of_equity"],
        "avg_exposure": metrics["avg_exposure"],
        "halted": metrics["halted"],
    }


def run_windows(
    data: MarketData,
    factory: Callable,
    params_list: list[dict],
    costs: dict[str, CostModel],
    limits: RiskLimits,
    spec: WindowSpec,
    starts: list[pd.Timestamp],
    processes: int | None = None,
) -> pd.DataFrame:
    """Run every (params, cost case, window) combination. Returns one row per run."""
    _STATE.update(data=data, factory=factory, costs=costs, limits=limits, spec=spec, params=params_list)
    tasks = [((index, case), start) for index in range(len(params_list)) for case in costs for start in starts]
    processes = processes or max(1, (os.cpu_count() or 2) - 2)
    if processes == 1 or len(tasks) < 8:
        rows = [_run_one(task) for task in tasks]
    else:
        with multiprocessing.get_context("fork").Pool(processes) as pool:
            rows = pool.map(_run_one, tasks, chunksize=max(1, len(tasks) // (processes * 8)))
    return pd.DataFrame(rows)


TARGET_RETURN = 0.002  # a window "succeeds" when it returns more than +0.2%


def window_summary(rows: pd.DataFrame, window_days: float = 3.0, target_return: float = TARGET_RETURN) -> dict:
    returns, drawdowns = rows["return"].to_numpy(), rows["max_drawdown"].to_numpy()
    if returns.size == 0:
        return {"windows": 0}
    return {
        "windows": int(returns.size),
        "p_above_target": float((returns > target_return).mean()),  # zero-trade windows count as failures
        "trades_per_day": float(rows["trades"].mean() / window_days),
        "positive_share": float((returns > 0).mean()),
        "median_return": float(np.median(returns)),
        "mean_return": float(returns.mean()),
        "p10_return": float(np.quantile(returns, 0.1)),
        "p90_return": float(np.quantile(returns, 0.9)),
        "worst_return": float(returns.min()),
        "p10_drawdown": float(np.quantile(drawdowns, 0.1)),
        "worst_drawdown": float(drawdowns.min()),
        "median_trades": float(np.median(rows["trades"])),
        "zero_trade_share": float((rows["trades"] == 0).mean()),
        "median_fees_share": float(np.median(rows["fees_share"])),
    }


@dataclass(frozen=True)
class Objective:
    """The user's objective (2026-10-22): maximize P(3-day return > +0.2%) subject to p10 drawdown
    >= -3.5% and at least 3 round trips per day on average. Zero-trade windows count as failures."""

    metric: str = "p_above_target"
    min_p10_drawdown: float = -0.035
    min_trades_per_day: float = 3.0

    def score(self, summary: dict) -> float:
        if summary.get("windows", 0) == 0:
            return float("-inf")
        if summary["p10_drawdown"] < self.min_p10_drawdown or summary["trades_per_day"] < self.min_trades_per_day:
            return float("-inf")
        return summary[self.metric]

    def feasible(self, summary: dict) -> dict:
        return {
            "drawdown_ok": summary.get("p10_drawdown", -1) >= self.min_p10_drawdown,
            "activity_ok": summary.get("trades_per_day", 0) >= self.min_trades_per_day,
        }


def objective(
    summary: dict, min_positive: float = 0.60, min_p10_drawdown: float = -0.04, metric: str = "median_return"
) -> float:
    """`metric` (median or mean 3-day return), only if the constraints hold; otherwise −inf."""
    if summary.get("windows", 0) == 0:
        return float("-inf")
    if summary["positive_share"] < min_positive or summary["p10_drawdown"] < min_p10_drawdown:
        return float("-inf")
    return summary[metric]


def buy_and_hold_windows(
    data: MarketData, symbol: str, starts: list[pd.Timestamp], spec: WindowSpec, costs: CostModel
) -> pd.DataFrame:
    """Benchmark: buy `symbol` with all equity at the window open (taker) and sell at the end (taker)."""
    frame = data.features[symbol]
    clock = data.clock
    slip = float(costs.slippage_for(symbol))
    taker = float(costs.fees.taker)
    rows = []
    for start in starts:
        first = int(clock.searchsorted(start, side="left"))
        last = int(clock.searchsorted(start + pd.Timedelta(days=spec.window_days), side="right")) - 1
        entry = frame["open"][first] * (1 + slip) * (1 + taker)
        path = frame["close"][first : last + 1] * (1 - slip) * (1 - taker) / entry
        peaks = np.maximum.accumulate(np.concatenate([[1.0], path]))[1:]
        rows.append(
            {
                "start": start.isoformat(),
                "return": float(path[-1] - 1),
                "max_drawdown": float(min(0.0, (path / peaks - 1).min())),
                "trades": 1,
                "fees_share": 2 * taker,
            }
        )
    return pd.DataFrame(rows)
