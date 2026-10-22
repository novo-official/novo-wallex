"""Phase 4 experiments with the local model: benchmark, plan, precompute, and the LLM ablation.

Ablation (3 configs, canonical = the trend rules): rules / + Regime Officer / + Officer + Trade Agent,
on non-overlapping 3-day windows (one every 3 days: every hour is seen once). The officer runs every
4 h in historical replay (hourly in live). Contexts are blind (no dates, no price levels).
Selection windows first; holdout once with --final. Every run is logged.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import statistics
import tempfile
import time
from pathlib import Path

from pydantic import ValidationError

from backtest.config import StudyConfig
from backtest.experiment import run_experiment, select_windows
from backtest.experiments import log_experiment
from backtest.report import write_json
from ml.p3_experiments import _manifest, build_market
from strategy.base import Context
from strategy.modules.trend import TrendModule, TrendParams

from . import replay
from .llm import LLMClient, ResponseCache, strict_schema
from .roles import RegimeDecision, RegimeOfficer, TradeAgent, TradeReview, market_context


def _config(config: StudyConfig) -> StudyConfig:
    return dataclasses.replace(config, windows=dataclasses.replace(config.windows, step_hours=72.0))


def _market(config: StudyConfig, data_dir: Path):
    return build_market(config, data_dir, [], replay.TRADED)


SUBSET_EVERY = 3  # time-boxed run (deadline 2026-10-23): every 3rd non-overlapping window, a small sample


def _windows(config: StudyConfig, data_dir: Path, final: bool):
    config = _config(config)
    data, first = _market(config, data_dir)
    selection, holdout = select_windows(data, config, first)
    selection, holdout = selection[::SUBSET_EVERY], holdout[::SUBSET_EVERY]
    return config, data, selection, holdout, selection + (holdout if final else [])


def _variants(config: StudyConfig, llm: str) -> list[tuple[str, dict]]:
    base = copy.deepcopy(config.strategy)
    officer = {**base, "agent": {"officer": True, "llm": llm}}
    both = {**base, "agent": {"officer": True, "trade_agent": True, "llm": llm}}
    return [("rules", base), ("rules + officer", officer), ("rules + officer + trade agent", both)]


def trend_candidates(data, symbols: list[str], params: dict, limit: int, stride: int = 600) -> list[tuple[int, object]]:
    """Rule entries (trend module) at spread-out bars, for the benchmark."""
    module = TrendModule(TrendParams(**params))
    ctx = Context(data.clock, data.features, data.symbols)
    found = []
    for index in range(3000, len(data.clock), stride):
        for offset in range(stride):
            ctx.index = index + offset
            if ctx.index >= len(data.clock):
                break
            intent = next((module.entry(ctx, s) for s in symbols if module.entry(ctx, s) is not None), None)
            if intent is not None:
                found.append((ctx.index, intent))
                break
        if len(found) >= limit:
            break
    return found


def benchmark(config: StudyConfig, data_dir: Path, out_dir: Path, reports_dir: Path, pairs: int = 5) -> dict:
    """2 x `pairs` officer + 2 x `pairs` trade-agent calls on the local model, bypassing the cache:
    latency, JSON validity on the first answer (no retry), and determinism (same input twice)."""
    _, data, selection, _, _ = _windows(config, data_dir, False)
    symbols = [s for s in replay.TRADED if s in data.features]
    llm_config = replay.load_llm_config()
    provider = replay.providers_for("live-local", llm_config)[0]
    with tempfile.TemporaryDirectory() as scratch:
        client = LLMClient(
            [provider],
            ResponseCache(Path(scratch) / "cache"),
            Path(scratch) / "journal.jsonl",
            options=dict(llm_config["options"]),
        )
        officer, agent = RegimeOfficer(client), TradeAgent(client)
        ctx = Context(data.clock, data.features, data.symbols)
        cases = []
        step = max(1, (len(selection) - 1) // pairs)
        for start in selection[::step][:pairs]:
            ctx.index = int(data.clock.searchsorted(start))
            cases.append(("officer", officer.first_request(market_context(ctx, symbols, blind=True)), RegimeDecision))
        for index, intent in trend_candidates(data, symbols, config.strategy["trend"], pairs):
            ctx.index = index
            market = market_context(ctx, symbols, blind=True)
            cases.append(("trade_agent", agent.first_request(intent, market, None), TradeReview))
        rows = []
        for role, request, schema in cases:
            outputs = []
            for attempt in range(2):
                started = time.monotonic()
                response = provider.create(request)
                seconds = time.monotonic() - started
                try:
                    parsed = schema.model_validate_json(response.get("text") or "").model_dump()
                    valid = True
                except ValidationError:
                    parsed, valid = None, False
                outputs.append(response.get("text"))
                rows.append(
                    {
                        "role": role,
                        "attempt": attempt,
                        "seconds": round(seconds, 2),
                        "valid_json": valid,
                        "input_tokens": response["usage"].get("input_tokens"),
                        "output_tokens": response["usage"].get("output_tokens"),
                        "output": parsed,
                    }
                )
            rows[-1]["deterministic"] = rows[-2]["deterministic"] = outputs[0] == outputs[1]
    latencies = [row["seconds"] for row in rows]
    summary = {
        "model": provider.identity.as_dict(),
        "calls": len(rows),
        "valid_json_rate": sum(row["valid_json"] for row in rows) / len(rows),
        "deterministic_pairs": sum(row["deterministic"] for row in rows[::2]) / (len(rows) // 2),
        "latency_s": {
            "median": statistics.median(latencies),
            "max": max(latencies),
            "mean": statistics.mean(latencies),
        },
        "by_role": {
            role: {
                "median_s": statistics.median([r["seconds"] for r in rows if r["role"] == role]),
                "valid": sum(r["valid_json"] for r in rows if r["role"] == role),
            }
            for role in ("officer", "trade_agent")
        },
        "schemas": {"officer": strict_schema(RegimeDecision), "trade_agent": strict_schema(TradeReview)},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "benchmark.json", {**summary, "rows": rows})
    log_experiment(
        reports_dir,
        "P4 local LLM benchmark (20 calls)",
        1,
        False,
        report=str(out_dir.relative_to(reports_dir.parent)),
        notes=f"valid {summary['valid_json_rate']:.0%}, deterministic {summary['deterministic_pairs']:.0%}",
    )
    return summary


def plan(config: StudyConfig, data_dir: Path, final: bool, seconds_per_call: float | None = None) -> dict:
    config, data, _, _, windows = _windows(config, data_dir, final)
    client = replay.make_client("replay")
    officer = replay.officer_plan(data, windows, config.windows.window_days, client)
    estimate = None
    if seconds_per_call:
        trade_calls = int(len(windows) * 6)  # about 6 rule entry intents per 3-day window (P2 decision logs)
        estimate = {
            "officer_s": round(len(officer["missing"]) * seconds_per_call),
            "trade_agent_s_upper": round(trade_calls * seconds_per_call),
            "hours_total": round((len(officer["missing"]) + trade_calls) * seconds_per_call / 3600, 2),
        }
    return {
        "windows": len(windows),
        **{k: v for k, v in officer.items() if k != "missing"},
        "to_compute": len(officer["missing"]),
        "estimate": estimate,
        "_missing": officer["missing"],
        "_data": data,
    }


def precompute(config: StudyConfig, data_dir: Path, final: bool, progress: Path) -> dict:
    planned = plan(config, data_dir, final)
    if not planned["_missing"]:
        return {"calls": 0, "note": "every officer call is cached"}
    return replay.precompute_officer(planned["_data"], planned["_missing"], replay.make_client("live-local"), progress)


def llm_ablation(config, data_dir, out_dir, reports_dir, final=False, llm="replay", log=True):
    config, data, _, _, _ = _windows(config, data_dir, final)
    _, first = _market(config, data_dir)
    planned = plan(config, data_dir, final)
    if planned["to_compute"] and llm == "replay":
        raise RuntimeError(f"{planned['to_compute']} officer calls are not cached: run `p4_experiment precompute`")
    return run_experiment(
        "P4 LLM ablation, local model (non-overlapping windows)",
        config,
        data,
        _manifest(data_dir),
        _variants(config, llm),
        out_dir,
        reports_dir,
        references=("rules", "rules + officer"),
        final=final,
        since=first,
        processes=1 if llm != "replay" else 4,
        notes=f"llm={llm}; officer every 4 h in replay; blind contexts",
        log=log,
    )


__all__ = ["benchmark", "plan", "precompute", "llm_ablation", "json"]
