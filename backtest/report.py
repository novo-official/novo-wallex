"""Writes run artifacts to reports/<run_id>/: config + hash, metrics, trades, decisions, equity, windows, charts."""

from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .engine import BacktestResult  # noqa: E402


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None if math.isnan(value) else ("inf" if value > 0 else "-inf")
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean(payload), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_run(directory: Path, result: BacktestResult, metrics: dict, config: dict, config_hash: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "config.json", {"config_hash": config_hash, "config": config})
    write_json(directory / "metrics.json", {"config_hash": config_hash, **metrics})
    trades = pd.DataFrame(
        [
            {
                "symbol": t.symbol,
                "module": t.module,
                "entry_time": t.entry_time.isoformat(),
                "exit_time": t.exit_time.isoformat(),
                "entry_price": str(t.entry_price),
                "exit_price": str(t.exit_price),
                "quantity": str(t.quantity),
                "fees": str(round(t.fees, 2)),
                "pnl": str(round(t.pnl, 2)),
                "r_multiple": round(t.r_multiple, 3),
                "exit_reason": t.exit_reason,
                "entry_decision": t.entry_decision,
            }
            for t in result.trades
        ]
    )
    trades.to_csv(directory / "trades.csv", index=False)
    with (directory / "decisions.jsonl").open("w", encoding="utf-8") as handle:
        for decision in result.decisions:
            handle.write(json.dumps(_clean(decision), sort_keys=True, default=str) + "\n")
    equity = pd.DataFrame({"equity": result.equity.round(2), "exposure": result.exposure.round(4)})
    equity.index.name = "time"
    equity.resample("1h").last().to_csv(directory / "equity_1h.csv")

    figure, axis = plt.subplots(figsize=(10, 4))
    (result.equity / float(result.initial_equity) - 1).mul(100).plot(ax=axis, linewidth=1)
    axis.set_title("Equity, % return in TMN")
    axis.set_ylabel("%")
    axis.grid(alpha=0.3)
    figure.tight_layout()
    figure.savefig(directory / "equity.png", dpi=120)
    plt.close(figure)


def write_window_charts(directory: Path, rows: pd.DataFrame, title: str) -> None:
    """Return histogram and return-vs-drawdown scatter, selection vs holdout."""
    directory.mkdir(parents=True, exist_ok=True)
    figure, (left, right) = plt.subplots(1, 2, figsize=(12, 4.5))
    for split, color in (("selection", "#4C78A8"), ("holdout", "#F58518")):
        part = rows[rows["split"] == split]
        if part.empty:
            continue
        left.hist(part["return"] * 100, bins=30, alpha=0.6, color=color, label=f"{split} ({len(part)})")
        right.scatter(part["max_drawdown"] * 100, part["return"] * 100, s=12, alpha=0.7, color=color, label=split)
    left.axvline(0, color="black", linewidth=0.8)
    left.set_xlabel("3-day return, %")
    left.set_ylabel("windows")
    left.legend()
    right.axhline(0, color="black", linewidth=0.8)
    right.set_xlabel("max drawdown, %")
    right.set_ylabel("3-day return, %")
    right.legend()
    figure.suptitle(title)
    figure.tight_layout()
    figure.savefig(directory / "windows.png", dpi=120)
    plt.close(figure)
