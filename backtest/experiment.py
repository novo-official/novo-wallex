"""One experiment = at most 30 strategy variants, chosen on selection windows, holdout only on request.

Rules (agreed 2026-10-22):
  * at most MAX_CONFIGS variants per experiment;
  * the choice uses the selection windows only, scored by the user's objective
    (P(3-day return > +0.2%) with p10 drawdown >= -3.5% and >= 3 round trips/day);
  * the holdout windows are run only with `final=True`, once per model, and that is logged;
  * every report has the 3-row fee table (low 0.15/0.20, base 0.25/0.35, stress x1.5).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from risk.limits import RiskLimits
from strategy.factory import build_strategy

from .config import StudyConfig, config_hash
from .engine import MarketData
from .experiments import log_experiment
from .report import write_json, write_window_charts
from .windows import buy_and_hold_windows, run_windows, split_selection_holdout, window_starts, window_summary

MAX_CONFIGS = 30
SUMMARY_KEYS = [
    "windows",
    "p_above_target",
    "trades_per_day",
    "positive_share",
    "mean_return",
    "median_return",
    "p10_return",
    "p90_return",
    "p10_drawdown",
    "worst_drawdown",
    "zero_trade_share",
    "median_fees_share",
]


def select_windows(data: MarketData, config: StudyConfig, since: pd.Timestamp | None = None):
    starts = window_starts(data.clock, config.windows)
    selection, holdout = split_selection_holdout(starts, config.windows, config.selection_share)
    if since is not None:
        selection = [start for start in selection if start >= since]
        holdout = [start for start in holdout if start >= since]
    return selection, holdout


def rank(rows: pd.DataFrame, names: list[str], config: StudyConfig) -> pd.DataFrame:
    table = []
    for index, name in enumerate(names):
        summary = window_summary(rows[rows["params_id"] == index], config.windows.window_days)
        table.append({"variant": name, "params_id": index, "score": config.objective.score(summary), **summary})
    return pd.DataFrame(table)


def choose(ranking: pd.DataFrame, config: StudyConfig, candidates: list[int] | None = None) -> tuple[int, bool]:
    """Best feasible variant; if none is feasible, the best P(>+0.2%) among drawdown-safe ones (flagged)."""
    pool = ranking if candidates is None else ranking[ranking["params_id"].isin(candidates)]
    feasible = pool[pool["score"] > float("-inf")]
    if not feasible.empty:
        best = feasible.sort_values(["score", "mean_return"]).iloc[-1]
        return int(best["params_id"]), True
    safe = pool[pool["p10_drawdown"] >= config.objective.min_p10_drawdown]
    best = (safe if not safe.empty else pool).sort_values(["p_above_target", "mean_return"]).iloc[-1]
    return int(best["params_id"]), False


def run_experiment(
    name: str,
    config: StudyConfig,
    data: MarketData,
    manifest: dict,
    variants: list[tuple[str, dict]],
    out_dir: Path,
    reports_dir: Path,
    *,
    references: tuple[str, ...] = (),
    final: bool = False,
    since: pd.Timestamp | None = None,
    factory: Callable = build_strategy,
    benchmark_data: dict[str, MarketData] | None = None,
    processes: int | None = None,
    notes: str = "",
    log: bool = True,
) -> dict:
    """Run `variants` on the selection windows, choose one, then the fee table (and holdout if `final`)."""
    if len(variants) > MAX_CONFIGS:
        raise ValueError(f"{name}: {len(variants)} variants; at most {MAX_CONFIGS} per experiment")
    for variant_name, variant_params in variants:
        if variant_params.get("ml_signal", {}).get("max_entries_per_day", 0) > 0:
            # User decision 2026-10-22: the daily-quota version is judged on forward (shadow) data only.
            raise ValueError(f"{name}: variant {variant_name!r} uses the activity quota, which is shadow-only")
    started = datetime.now(UTC)
    names = [variant_name for variant_name, _ in variants]
    params = [variant_params for _, variant_params in variants]
    selection, holdout = select_windows(data, config, since)
    limits = RiskLimits()

    rows = run_windows(data, factory, params, {"base": config.costs()}, limits, config.windows, selection, processes)
    ranking = rank(rows, names, config)
    candidate_ids = [i for i, variant_name in enumerate(names) if variant_name not in references]
    chosen_id, feasible = choose(ranking, config, candidate_ids)
    report_ids = [chosen_id] + [names.index(reference) for reference in references if reference in names]

    windows = selection + (holdout if final else [])
    table_rows = run_windows(
        data, factory, [params[i] for i in report_ids], config.cost_cases(), limits, config.windows, windows, processes
    )
    holdout_set = {start.isoformat() for start in holdout}
    table_rows["split"] = table_rows["start"].map(lambda s: "holdout" if s in holdout_set else "selection")
    table_rows["variant"] = table_rows["params_id"].map(lambda i: names[report_ids[i]])
    fee_table = []
    for (variant, case, split), part in table_rows.groupby(["variant", "cost_case", "split"]):
        summary = window_summary(part, config.windows.window_days)
        fee_table.append(
            {"variant": variant, "split": split, "fees": case, **{k: summary.get(k) for k in SUMMARY_KEYS}}
        )
    fee_frame = pd.DataFrame(fee_table)

    benchmarks = []
    for label, bench_data in (benchmark_data or {"raw": data}).items():
        if "USDTTMN" not in bench_data.features:
            continue
        bench = buy_and_hold_windows(bench_data, "USDTTMN", windows, config.windows, config.costs())
        bench["split"] = bench["start"].map(lambda s: "holdout" if s in holdout_set else "selection")
        for split, part in bench.groupby("split"):
            summary = window_summary(part, config.windows.window_days)
            benchmarks.append({"benchmark": f"buy_and_hold_USDTTMN_{label}", "split": split, **summary})

    digest, payload = config_hash(config, params[chosen_id], manifest)
    out_dir.mkdir(parents=True, exist_ok=True)
    ranking.to_csv(out_dir / "variants_selection.csv", index=False)
    fee_frame.to_csv(out_dir / "fee_table.csv", index=False)
    table_rows.to_csv(out_dir / "windows.csv", index=False)
    pd.DataFrame(benchmarks).to_csv(out_dir / "benchmarks.csv", index=False)
    summary = {
        "experiment": name,
        "config_hash": digest,
        "variants": len(variants),
        "chosen": names[chosen_id],
        "chosen_params": params[chosen_id],
        "chosen_feasible": feasible,
        "objective": config.objective.__dict__,
        "holdout_evaluated": final,
        "selection_windows": len(selection),
        "holdout_windows": len(holdout) if final else 0,
        "runtime_seconds": (datetime.now(UTC) - started).total_seconds(),
    }
    write_json(out_dir / "summary.json", summary)
    write_json(out_dir / "config.json", {"config_hash": digest, "config": payload})
    base = table_rows[(table_rows["cost_case"] == "base") & (table_rows["variant"] == names[chosen_id])]
    write_window_charts(out_dir, base, f"{name}: {names[chosen_id]} (base fees)")
    if log:
        log_experiment(
            reports_dir,
            name,
            len(variants),
            final,
            report=str(out_dir.relative_to(reports_dir.parent)) if out_dir.is_relative_to(reports_dir.parent) else None,
            notes=notes or f"chosen: {names[chosen_id]}",
            config_hash=digest,
        )
    return {"summary": summary, "ranking": ranking, "fee_table": fee_frame, "benchmarks": pd.DataFrame(benchmarks)}


def print_tables(result: dict, write: Callable[[str], None]) -> None:
    columns = [
        "variant",
        "p_above_target",
        "trades_per_day",
        "mean_return",
        "median_return",
        "p10_drawdown",
        "worst_drawdown",
        "zero_trade_share",
        "score",
    ]
    write(result["ranking"][columns].round(4).to_string(index=False))
    write(json.dumps({k: result["summary"][k] for k in ("chosen", "chosen_feasible", "holdout_evaluated")}))
    write(result["fee_table"].round(4).to_string(index=False))
    if not result["benchmarks"].empty:
        write(
            result["benchmarks"][
                ["benchmark", "split", "p_above_target", "mean_return", "p10_drawdown", "worst_drawdown"]
            ]
            .round(4)
            .to_string(index=False)
        )
