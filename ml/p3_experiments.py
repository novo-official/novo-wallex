"""Phase 3 experiments, one function per experiment, each logged in reports/experiments.jsonl.

Run with `python manage.py p3_experiment <name> [--final]`. The holdout is evaluated only
with --final (once per model, at the end). Every experiment stays at or below 30 configs.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from backtest.config import StudyConfig
from backtest.dataset import build_features
from backtest.experiments import log_experiment
from backtest.report import write_json

from .dataset import ml_frames
from .features import FEATURES
from .perbar import PerBarSpec, Target, build_table, oof_quality, walk_forward_net

TRADED = ["USDTTMN", "BTCTMN", "ETHTMN"]
TARGETS = [
    Target("h12", "horizon", 12),
    Target("h24", "horizon", 24),
    Target("h48", "horizon", 48),
    Target("tb48", "barrier", 48, stop=0.01, take_profit=0.02),
]


def load_features(config: StudyConfig, data_dir: Path) -> tuple[dict, dict]:
    symbols = list(config.tradable) + [s.removesuffix("TMN") + "USDT" for s in config.tradable if s != "USDTTMN"]
    features = build_features(data_dir, symbols)
    frames = ml_frames(features, list(config.tradable), list(config.tradable))
    return features, frames


def selection_end(config: StudyConfig, features: dict) -> pd.Timestamp:
    """Start of the holdout period: nothing at or after it is looked at before --final."""
    from backtest.windows import split_selection_holdout, window_starts

    clock = pd.DatetimeIndex(features["BTCTMN"].index)
    starts = window_starts(clock, config.windows)
    _, holdout = split_selection_holdout(starts, config.windows, config.selection_share)
    return holdout[0]


def ml_targets(config: StudyConfig, data_dir: Path, out_dir: Path, reports_dir: Path, symbols=None) -> dict:
    """Which target has out-of-sample skill net of costs? Selection period only."""
    features, frames = load_features(config, data_dir)
    symbols = symbols or TRADED
    slippage = {s: float(v) for s, v in config.slippage.items()}
    spec = PerBarSpec()
    until = selection_end(config, features)
    results = {}
    for target in TARGETS:
        table = build_table(features, frames, symbols, target, slippage, spec, int(config.windows.warmup_days * 288))
        predictions, blocks = walk_forward_net(table, {s: frames[s] for s in symbols}, FEATURES, target, spec)
        results[target.name] = {"target": target.__dict__, **oof_quality(table, predictions, until)}
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "targets.json", {"symbols": symbols, "selection_until": until.isoformat(), **results})
    log_experiment(
        reports_dir,
        f"P3b ML target choice ({'/'.join(symbols)})",
        len(TARGETS),
        False,
        report=str(out_dir.relative_to(reports_dir.parent)),
        notes="OOF on the selection period only",
    )
    return results


def predictions_for(config: StudyConfig, features: dict, frames: dict, target: Target, symbols: list[str]):
    slippage = {s: float(v) for s, v in config.slippage.items()}
    spec = PerBarSpec()
    table = build_table(features, frames, symbols, target, slippage, spec, int(config.windows.warmup_days * 288))
    predictions, _ = walk_forward_net(table, {s: frames[s] for s in symbols}, FEATURES, target, spec)
    return table, predictions


def summarize_targets(results: dict) -> pd.DataFrame:
    return pd.DataFrame({name: {k: v for k, v in r.items() if k != "target"} for name, r in results.items()}).T


__all__ = ["TARGETS", "TRADED", "ml_targets", "predictions_for", "summarize_targets", "np"]


# --- shared data assembly -------------------------------------------------------------------------

CACHE = Path(__file__).resolve().parent.parent / ".cache" / "ml"


def _cache_key(data_dir: Path, target: Target, symbols: list[str], detrended: bool) -> str:
    import hashlib
    import json

    from .study import _manifest_digest

    payload = {
        "data": _manifest_digest(data_dir),
        "target": target.__dict__,
        "symbols": symbols,
        "spec": PerBarSpec().__dict__,
        "features": FEATURES,
        "detrended": detrended,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def build_market(
    config: StudyConfig, data_dir: Path, targets: list[Target], ml_symbols: list[str], detrended: bool = False
):
    """MarketData with walk-forward columns ml_net_<target> for `ml_symbols` (cached on disk)."""
    from backtest.dataset import build_market_data, detrend, load_rules

    transform = (lambda symbol, bars: detrend(bars) if symbol == "USDTTMN" else bars) if detrended else None
    symbols = list(config.tradable) + [s.removesuffix("TMN") + "USDT" for s in config.tradable if s != "USDTTMN"]
    features = build_features(data_dir, symbols, transform)
    frames = ml_frames(features, list(config.tradable), list(config.tradable))
    for target in targets:
        path = CACHE / f"{target.name}-{_cache_key(data_dir, target, ml_symbols, detrended)}.parquet"
        if path.exists():
            cached = pd.read_parquet(path)
        else:
            _, predictions = predictions_for(config, features, frames, target, ml_symbols)
            cached = pd.DataFrame(predictions)
            CACHE.mkdir(parents=True, exist_ok=True)
            cached.to_parquet(path)
        for symbol in features:
            column = cached[symbol] if symbol in cached else np.nan
            features[symbol][f"ml_net_{target.name}"] = column
    first = max(pd.Series(cached[s]).first_valid_index() for s in ml_symbols) if targets else None
    return build_market_data(features, config.tradable, load_rules(data_dir)), first


def _manifest(data_dir: Path) -> dict:
    from .study import _manifest

    return _manifest(data_dir)


def _rules(config: StudyConfig, **trend) -> dict:
    import copy

    params = copy.deepcopy(config.strategy)
    params["trend"] = {**params.get("trend", {}), **trend}
    return params


def _ml_only(
    column: str,
    threshold: float,
    hold: int,
    symbols: list[str],
    take_profit: float = 0.0,
    stop: float = 0.015,
    exit_type: str = "taker",
) -> dict:
    return {
        "modules": ["ml_signal"],
        "symbols": symbols,
        "ml_signal": {
            "column": column,
            "threshold": threshold,
            "hold_bars": hold,
            "take_profit": take_profit,
            "stop": stop,
            "exit_type": exit_type,
        },
    }


def print_result(result: dict, write) -> None:
    from backtest.experiment import print_tables

    print_tables(result, write)


# --- experiments ------------------------------------------------------------------------------------


def entry_cost(config, data_dir, out_dir, reports_dir, final=False):
    """Maker-limit entries vs taker entries for the P2 rules: how much does maker execution save?"""
    from backtest.experiment import run_experiment

    data, _ = build_market(config, data_dir, [], TRADED)
    variants = [
        ("rules maker entry", _rules(config, entry_type="maker")),
        ("rules taker entry", _rules(config, entry_type="taker")),
    ]
    return run_experiment(
        "P3b entry cost: maker vs taker entries",
        config,
        data,
        _manifest(data_dir),
        variants,
        out_dir,
        reports_dir,
        references=("rules taker entry",),
        final=final,
    )


def rules_grid(config, data_dir, out_dir, reports_dir, final=False):
    """The P2 rule grid (24 combos) under the new objective."""
    from backtest.config import expand_grid
    from backtest.experiment import run_experiment

    data, _ = build_market(config, data_dir, [], TRADED)
    grid = expand_grid(config.strategy, config.grid)
    variants = [(f"rules grid#{i}", params) for i, params in enumerate(grid)]
    return run_experiment(
        "P3b rules grid under the new objective",
        config,
        data,
        _manifest(data_dir),
        variants,
        out_dir,
        reports_dir,
        final=final,
    )


ML_TARGETS = [t for t in TARGETS if t.name in ("h24", "h48", "tb48")]


def ml_only(config, data_dir, out_dir, reports_dir, final=False):
    """ML-only: enter when E[net] > threshold (3 targets x 4 thresholds = 12 configs)."""
    from backtest.experiment import run_experiment

    data, first = build_market(config, data_dir, ML_TARGETS, TRADED)
    variants = []
    for target in ML_TARGETS:
        for threshold in (-0.002, -0.001, 0.0, 0.001):
            take_profit = target.take_profit if target.kind == "barrier" else 0.0
            stop = target.stop if target.kind == "barrier" else 0.015
            params = _ml_only(f"ml_net_{target.name}", threshold, target.horizon, TRADED, take_profit, stop)
            variants.append((f"ML {target.name} E[net]>{threshold * 100:+.1f}%", params))
    variants.append(("rules (reference)", _rules(config)))
    return run_experiment(
        "P3b ML-only",
        config,
        data,
        _manifest(data_dir),
        variants,
        out_dir,
        reports_dir,
        references=("rules (reference)",),
        final=final,
        since=first,
    )


def rules_plus_ml(config, data_dir, out_dir, reports_dir, final=False):
    """Rules + ML: (a) both modules trade, (b) rule entries gated by the ML E[net]."""
    from backtest.experiment import run_experiment

    data, first = build_market(config, data_dir, ML_TARGETS, TRADED)
    variants = []
    for target_name, hold in (("h48", 48), ("tb48", 48)):
        target = next(t for t in ML_TARGETS if t.name == target_name)
        for threshold in (-0.001, 0.0):
            params = _rules(config)
            params["modules"] = ["trend", "ml_signal"]
            params["ml_signal"] = _ml_only(
                f"ml_net_{target_name}",
                threshold,
                hold,
                TRADED,
                target.take_profit if target.kind == "barrier" else 0.0,
                target.stop if target.kind == "barrier" else 0.015,
            )["ml_signal"]
            variants.append((f"rules + ML {target_name} >{threshold * 100:+.1f}%", params))
    for threshold in (-0.004, -0.003, -0.002):
        params = _rules(config)
        params["ml"] = {"enabled": True, "column": "ml_net_h48", "threshold": threshold}
        variants.append((f"rules gated by ML h48 >{threshold * 100:+.1f}%", params))
    variants.append(("rules (reference)", _rules(config)))
    return run_experiment(
        "P3b rules + ML",
        config,
        data,
        _manifest(data_dir),
        variants,
        out_dir,
        reports_dir,
        references=("rules (reference)",),
        final=final,
        since=first,
    )


def _usdt_variants():
    variants = []
    for regime in ("4h", "1h200", "4h200"):
        for stop_atr in (2.0, 3.0):
            for cap in (0.20, 0.40):
                params = {
                    "modules": ["usdt_allocation"],
                    "symbols": ["USDTTMN"],
                    "usdt_allocation": {"regime": regime, "stop_atr": stop_atr},
                }
                if cap != 0.20:
                    params["risk"] = {"position_caps": {"USDTTMN": cap}}
                variants.append((f"USDT {regime} stop {stop_atr:g}xATR cap {cap:.0%}", params))
    return variants


def usdt_allocation(config, data_dir, out_dir, reports_dir, final=False, detrended=False):
    """USDT regime allocation (12 configs). Benchmarks: buy-and-hold USDTTMN, raw and detrended."""
    from backtest.experiment import run_experiment

    data, _ = build_market(config, data_dir, [], TRADED, detrended=detrended)
    flat, _ = build_market(config, data_dir, [], TRADED, detrended=not detrended)
    benchmarks = {"detrended": data, "raw": flat} if detrended else {"raw": data, "detrended": flat}
    label = "detrended USDTTMN" if detrended else "raw"
    return run_experiment(
        f"P3b USDT regime allocation ({label})",
        config,
        data,
        _manifest(data_dir),
        _usdt_variants(),
        out_dir,
        reports_dir,
        final=final,
        benchmark_data=benchmarks,
    )


def usdt_allocation_detrended(config, data_dir, out_dir, reports_dir, final=False):
    return usdt_allocation(config, data_dir, out_dir, reports_dir, final=final, detrended=True)


def solxrp_maker(config, data_dir, out_dir, reports_dir, final=False):
    """SOL/XRP with maker-only execution (maker entries and maker exits, stops stay taker)."""
    from backtest.experiment import run_experiment

    symbols = ["SOLTMN", "XRPTMN"]
    target = next(t for t in ML_TARGETS if t.name == "h48")
    data, first = build_market(config, data_dir, [target], symbols)
    variants = []
    for exit_type in ("maker", "taker"):
        rules = _rules(config)
        rules["symbols"] = symbols
        variants.append((f"rules SOL/XRP {exit_type} exits", rules))  # trend exits are stops/TP; see note
        for threshold in (-0.001, 0.0):
            variants.append(
                (
                    f"ML h48 SOL/XRP >{threshold * 100:+.1f}% {exit_type} exits",
                    _ml_only("ml_net_h48", threshold, 48, symbols, exit_type=exit_type),
                )
            )
    return run_experiment(
        "P3b SOL/XRP maker-only execution",
        config,
        data,
        _manifest(data_dir),
        variants[1:],
        out_dir,
        reports_dir,
        final=final,
        since=first,
        notes="rules on SOL/XRP exit by stop/take-profit (maker TP), so only ML varies exits",
    )


def explain_h48(config, data_dir, out_dir, reports_dir, final=False):
    """Fit the h48 model on all labels (for live use later), save it, and write the SHAP summary."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from .model import CalibratedModel, save
    from .perbar import fit_net_model, shap_summary
    from .study import _manifest_digest

    features, frames = load_features(config, data_dir)
    target = next(t for t in ML_TARGETS if t.name == "h48")
    slippage = {s: float(v) for s, v in config.slippage.items()}
    spec = PerBarSpec()
    table = build_table(features, frames, TRADED, target, slippage, spec, int(config.windows.warmup_days * 288))
    model = fit_net_model(table, FEATURES, target, spec)
    shap = shap_summary(model, table[FEATURES])
    out_dir.mkdir(parents=True, exist_ok=True)
    shap.to_csv(out_dir / "shap_summary.csv", index=False)
    top = shap.head(15).iloc[::-1]
    figure, axis = plt.subplots(figsize=(7, 5))
    axis.barh(
        top["feature"], top["mean_abs_shap"] * 100, color=["#E45756" if d < 0 else "#4C78A8" for d in top["direction"]]
    )
    axis.set_xlabel("mean |SHAP|, % of predicted 4h return (blue: higher value -> higher prediction)")
    axis.set_title("ML h48: what drives the prediction (TreeSHAP)")
    figure.tight_layout()
    figure.savefig(out_dir / "shap_summary.png", dpi=120)
    plt.close(figure)
    wrapped = CalibratedModel(model.booster, model.calibrator, FEATURES, {"base_rate": None})
    model_id = save(
        wrapped,
        Path(__file__).resolve().parent.parent / "models" / "ml_net_h48",
        {
            "target": target.__dict__,
            "symbols": TRADED,
            "spec": spec.__dict__,
            "output": "calibrated E[net return]",
            "trained_until": pd.Timestamp(table["t_end"].max()).isoformat(),
            "data_manifest_sha256": _manifest_digest(data_dir),
            "status": "not live: see docs/RESEARCH_P3.md",
        },
    )
    log_experiment(
        reports_dir,
        "P3b final h48 model fit + SHAP (no evaluation)",
        1,
        False,
        report=str(out_dir.relative_to(reports_dir.parent)),
        notes=f"model {model_id}",
    )
    return {"model_id": model_id, "shap": shap}


def activity_cost(config, data_dir, out_dir, reports_dir, final=False):
    """Cheapest way to reach >= 3 round trips/day: the rules at full size plus small ML-signal trades.

    Every extra trade has a real (if weak) signal: the h48/h24 model's E[net] above a threshold.
    Pure churn (buy and sell without a signal) is not tested: it risks counting as artificial volume.
    Size is the main cost driver, so the activity trades are 100k / 250k / 500k TMN; below our 500k
    default minimum the variant sets min_order_tmn to the exchange minimum of 50k TMN.
    """
    from backtest.experiment import run_experiment

    targets = [t for t in ML_TARGETS if t.name in ("h24", "h48")]
    data, first = build_market(config, data_dir, targets, TRADED)
    variants = [("rules only (reference)", _rules(config))]
    for target_name, hold in (("h48", 48), ("h24", 24)):
        thresholds = (-0.005, -0.004, -0.003) if target_name == "h48" else (-0.005, -0.004)
        for threshold in thresholds:
            for notional in (100_000, 250_000, 500_000):
                params = _rules(config)
                params["modules"] = ["trend", "ml_signal"]
                probe = _ml_only(f"ml_net_{target_name}", threshold, hold, TRADED)["ml_signal"]
                probe.update({"min_fraction": 1.0, "size_scale": notional / 1_000_000})  # fixed size
                params["ml_signal"] = probe
                if notional < 500_000:
                    params["risk"] = {"min_order_tmn": 50_000}
                variants.append((f"rules + {target_name} >{threshold * 100:+.1f}% at {notional // 1000}k", params))
    return run_experiment(
        "P3c activity cost: rules + small ML-signal trades",
        config,
        data,
        _manifest(data_dir),
        variants,
        out_dir,
        reports_dir,
        references=("rules only (reference)",),
        final=final,
        since=first,
        notes="cheapest way to reach >= 3 round trips/day",
    )
