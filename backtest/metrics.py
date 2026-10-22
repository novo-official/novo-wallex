"""Performance metrics of one backtest run."""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal

import numpy as np
import pandas as pd

from .engine import BacktestResult


def max_drawdown(equity: pd.Series) -> float:
    """Most negative (equity / running peak − 1). 0 for a curve that never falls."""
    values = equity.to_numpy(dtype=float)
    if values.size == 0:
        return 0.0
    peaks = np.maximum.accumulate(values)
    return float(np.min(values / peaks - 1.0))


def _annualized(returns: np.ndarray, periods_per_year: float, downside: bool = False) -> float:
    if returns.size < 2:
        return 0.0
    deviation = returns[returns < 0].std(ddof=0) if downside else returns.std(ddof=0)
    if downside and (returns < 0).sum() < 2:
        return 0.0
    if deviation == 0:
        return 0.0
    return float(returns.mean() / deviation * math.sqrt(periods_per_year))


def compute_metrics(result: BacktestResult) -> dict:
    equity = result.equity
    days = max((result.end - result.start).total_seconds() / 86400, 1e-9)
    hourly = equity.resample("1h").last().dropna()
    hourly_returns = hourly.pct_change().dropna().to_numpy()
    pnls = [float(trade.pnl) for trade in result.trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl <= 0]
    by_module: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for trade in result.trades:
        bucket = by_module[trade.module]
        bucket["trades"] += 1
        bucket["pnl"] += float(trade.pnl)
        bucket["wins"] += trade.pnl > 0
    exit_reasons: dict[str, int] = defaultdict(int)
    for trade in result.trades:
        exit_reasons[trade.exit_reason] += 1
    initial = float(result.initial_equity)
    return {
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "days": round(days, 3),
        "return": float(result.final_equity / result.initial_equity - 1),
        "final_equity": float(result.final_equity),
        "max_drawdown": max_drawdown(equity),
        "trades": len(pnls),
        "trades_per_day": len(pnls) / days,
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (math.inf if wins else 0.0),
        "avg_r": float(np.mean([trade.r_multiple for trade in result.trades])) if result.trades else 0.0,
        "sharpe": _annualized(hourly_returns, 24 * 365),
        "sortino": _annualized(hourly_returns, 24 * 365, downside=True),
        "fees_paid": float(result.fees_paid),
        "fees_share_of_equity": float(result.fees_paid) / initial,
        "avg_exposure": float(result.exposure.mean()),
        "final_tmn_share": float(result.final_tmn_share),
        "halted": result.halted,
        "by_module": {name: dict(values) for name, values in by_module.items()},
        "exit_reasons": dict(exit_reasons),
    }


def as_decimal(value: float) -> Decimal:
    return Decimal(repr(value))
