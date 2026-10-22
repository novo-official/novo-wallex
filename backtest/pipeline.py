"""End-to-end runs used by the commands and by `make reproduce`: load data, run, summarize, write reports."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from risk.limits import RiskLimits
from strategy.factory import build_strategy

from .config import StudyConfig, config_hash, expand_grid
from .dataset import build_features, build_market_data, load_rules
from .engine import Backtester, MarketData
from .experiment import run_experiment
from .metrics import compute_metrics
from .report import write_run


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


def window_study(
    config: StudyConfig,
    data: MarketData,
    manifest: dict,
    out_dir: Path,
    reports_dir: Path,
    processes: int | None = None,
    final: bool = False,
) -> dict:
    """The [grid] of config/strategy.toml as one experiment (at most 30 combinations)."""
    grid = expand_grid(config.strategy, config.grid)
    variants = [(f"grid#{index}", params) for index, params in enumerate(grid)]
    return run_experiment(
        "window_study grid", config, data, manifest, variants, out_dir, reports_dir, final=final, processes=processes
    )
