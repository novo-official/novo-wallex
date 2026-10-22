"""End-to-end runs used by the commands and by `make reproduce`: load data, run, summarize, write reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from risk.limits import RiskLimits
from strategy.factory import build_strategy

from .config import StudyConfig, config_hash, expand_grid
from .dataset import build_features, build_market_data, load_rules
from .engine import Backtester, MarketData
from .metrics import compute_metrics
from .report import write_json, write_run, write_window_charts
from .windows import (
    buy_and_hold_windows,
    objective,
    run_windows,
    split_selection_holdout,
    window_starts,
    window_summary,
)


def load_market(config: StudyConfig, data_dir: Path) -> tuple[MarketData, dict]:
    symbols = list(config.tradable)
    symbols += [s.removesuffix("TMN") + "USDT" for s in config.tradable if s != "USDTTMN"]  # fair-value legs
    features = build_features(data_dir, symbols)
    data = build_market_data(features, config.tradable, load_rules(data_dir))
    manifest_path = data_dir / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    return data, manifest


def run_single(
    config: StudyConfig,
    data: MarketData,
    manifest: dict,
    params: dict,
    start: pd.Timestamp,
    end: pd.Timestamp,
    out_dir: Path | None,
    stressed: bool = False,
) -> dict:
    digest, payload = config_hash(config, params, manifest)
    strategy = build_strategy(params, data.symbols)
    result = Backtester(data, strategy, config.costs(stressed), RiskLimits(), config.initial_equity).run(start, end)
    metrics = compute_metrics(result)
    if out_dir is not None:
        write_run(out_dir, result, metrics, {**payload, "fee_stress": stressed}, digest)
    return {"config_hash": digest, **metrics}


def window_study(config: StudyConfig, data: MarketData, manifest: dict, out_dir: Path, processes: int | None = None):
    """Grid on selection windows, then the chosen parameters (and benchmarks) on the holdout, both cost cases."""
    starts = window_starts(data.clock, config.windows)
    selection, holdout = split_selection_holdout(starts, config.windows, config.selection_share)
    grid = expand_grid(config.strategy, config.grid)
    base_costs = {"base": config.costs()}

    started = datetime.now(UTC)
    grid_rows = run_windows(data, build_strategy, grid, base_costs, RiskLimits(), config.windows, selection, processes)
    ranking = []
    for index, params in enumerate(grid):
        summary = window_summary(grid_rows[grid_rows["params_id"] == index])
        score = objective(summary, config.min_positive, config.min_p10_drawdown)
        ranking.append({"params_id": index, "objective": score, **summary, "params": json.dumps(params)})
    ranking_frame = pd.DataFrame(ranking).sort_values(["objective", "median_return"], ascending=False)
    feasible = ranking_frame[ranking_frame["objective"] > float("-inf")]
    chosen_id = int((feasible if not feasible.empty else ranking_frame).iloc[0]["params_id"])
    chosen = grid[chosen_id]

    final_rows = run_windows(
        data,
        build_strategy,
        [chosen],
        config.cost_cases(),
        RiskLimits(),
        config.windows,
        selection + holdout,
        processes,
    )
    holdout_set = {start.isoformat() for start in holdout}
    final_rows["split"] = final_rows["start"].map(lambda s: "holdout" if s in holdout_set else "selection")

    summaries: dict[str, dict] = {}
    for (case, split), part in final_rows.groupby(["cost_case", "split"]):
        summaries[f"{split}/{case}"] = window_summary(part)
    benchmarks = {}
    for symbol in ("USDTTMN", "BTCTMN"):
        if symbol in data.features:
            bench = buy_and_hold_windows(data, symbol, selection + holdout, config.windows, config.costs())
            bench["split"] = bench["start"].map(lambda s: "holdout" if s in holdout_set else "selection")
            for split, part in bench.groupby("split"):
                benchmarks[f"buy_and_hold_{symbol}/{split}"] = window_summary(part)

    digest, payload = config_hash(config, chosen, manifest)
    out_dir.mkdir(parents=True, exist_ok=True)
    ranking_frame.to_csv(out_dir / "grid.csv", index=False)
    final_rows.to_csv(out_dir / "windows.csv", index=False)
    summary = {
        "config_hash": digest,
        "chosen_params_id": chosen_id,
        "chosen_feasible": not feasible.empty,
        "grid_size": len(grid),
        "selection_windows": len(selection),
        "holdout_windows": len(holdout),
        "selection_period": [selection[0].isoformat(), selection[-1].isoformat()] if selection else None,
        "holdout_period": [holdout[0].isoformat(), holdout[-1].isoformat()] if holdout else None,
        "results": summaries,
        "benchmarks": benchmarks,
        "runtime_seconds": (datetime.now(UTC) - started).total_seconds(),
    }
    write_json(out_dir / "summary.json", summary)
    write_json(out_dir / "config.json", {"config_hash": digest, "config": payload})
    base = final_rows[final_rows["cost_case"] == "base"]
    write_window_charts(out_dir, base, f"3-day windows, chosen params #{chosen_id} (base fees)")
    return summary, chosen
