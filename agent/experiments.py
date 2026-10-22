"""Phase 4 experiments: the LLM ablation on non-overlapping 3-day windows.

Variants (3 configs, all on the canonical strategy = trend rules + 100k activity trades):
  * no LLM;
  * + Regime/Risk Officer (hourly, replayed from the batch-precomputed cache);
  * + Officer + Trade Agent (core entries reviewed; calls the API only for entries that happen).
Non-overlapping windows (one every 3 days) mean every hour is seen once, so the officer's calls can
be precomputed in one batch, and the cost stays bounded. Selection windows first; holdout once.
"""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

from backtest.config import StudyConfig
from backtest.experiment import run_experiment, select_windows
from ml.p3_experiments import _manifest, build_market

from . import replay

ML_TARGET = "h24"


def _config(config: StudyConfig) -> StudyConfig:
    return dataclasses.replace(config, windows=dataclasses.replace(config.windows, step_hours=72.0))


def _market(config: StudyConfig, data_dir: Path):
    from ml.p3_experiments import TARGETS

    return build_market(config, data_dir, [t for t in TARGETS if t.name == ML_TARGET], replay.TRADED)


def _variants(config: StudyConfig, agent_mode: str) -> list[tuple[str, dict]]:
    base = copy.deepcopy(config.strategy)
    officer = {**base, "agent": {"officer": True, "officer_mode": "replay"}}
    both = {**base, "agent": {"officer": True, "officer_mode": "replay", "trade_agent": True, "agent_mode": agent_mode}}
    return [("canonical, no LLM", base), ("canonical + officer", officer), ("canonical + officer + trade agent", both)]


def llm_plan(config, data_dir, final: bool = False) -> dict:
    """What the ablation needs from the officer, what is cached, and what the rest would cost."""
    config = _config(config)
    data, first = _market(config, data_dir)
    selection, holdout = select_windows(data, config, first)
    windows = selection + (holdout if final else [])
    requests = replay.officer_requests(data, windows, config.windows.window_days)
    todo = replay.missing(requests)
    return {
        "windows": len(windows),
        "officer_requests": len(requests),
        "cached": len(requests) - len(todo),
        "to_buy": replay.estimate(todo),
        "requests": todo,
    }


def llm_ablation(config, data_dir, out_dir, reports_dir, final=False, agent_mode="live"):
    config = _config(config)
    data, first = _market(config, data_dir)
    plan = llm_plan(config, data_dir, final=final)
    if plan["to_buy"]["requests"]:
        raise RuntimeError(f"{plan['to_buy']['requests']} officer requests are not cached; run llm_submit first")
    return run_experiment(
        "P4 LLM ablation (non-overlapping windows)",
        config,
        data,
        _manifest(data_dir),
        _variants(config, agent_mode),
        out_dir,
        reports_dir,
        references=("canonical, no LLM",),
        final=final,
        since=first,
        processes=4,
        notes="officer replayed from batch cache; trade agent live for entries that happen",
    )
