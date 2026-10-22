"""ML ablation: rules-only vs rules + ML filter (+ sizing), on the same 3-day windows.

1. Walk-forward, purged, calibrated LightGBM per label definition -> out-of-sample `ml_edge_<label>`
   for every bar of the traded symbols (each bar predicted by a model trained only on earlier labels).
2. Every variant (rules; ML filter at each threshold, with and without sizing) runs on the same
   windows. Only windows that start after the first ML prediction are used, for every variant.
3. Label, threshold and sizing are chosen on the selection windows; the holdout windows report.
4. The final model of the chosen label is refit on all samples and saved under models/.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.metrics import brier_score_loss, roc_auc_score  # noqa: E402

from backtest.config import StudyConfig, config_hash  # noqa: E402
from backtest.dataset import build_features, build_market_data, load_rules  # noqa: E402
from backtest.report import write_json  # noqa: E402
from backtest.windows import objective, run_windows, split_selection_holdout, window_starts, window_summary  # noqa: E402
from risk.limits import RiskLimits  # noqa: E402
from strategy.factory import build_strategy  # noqa: E402

from .dataset import build_samples, ml_frames  # noqa: E402
from .labels import BarrierSpec  # noqa: E402
from .model import WalkForwardSpec, calibration_table, fit, save, walk_forward  # noqa: E402

BAR = pd.Timedelta(minutes=5)
RANDOM_CONTROLS = 5


def _pass_rate(features: dict, symbols: list[str], column: str, threshold: float, since: pd.Timestamp) -> float:
    """Share of rule-trigger bars (1h uptrend + 5m breakout) whose ML value passes the threshold."""
    from .features import breakout_trigger, uptrend_1h

    passed = total = 0
    for symbol in symbols:
        frame = features[symbol]
        trigger = (uptrend_1h(frame) & breakout_trigger(frame)) & (frame.index >= since)
        values = frame.loc[trigger, column]
        total += int(values.notna().sum())
        passed += int((values >= threshold).sum())
    return passed / total if total else 0.0


def _oof_metrics(samples, edges: dict[str, pd.Series], symbols: list[str], split_time: pd.Timestamp) -> dict:
    mask = np.isin(samples.symbol, symbols)
    edge = np.array([edges[s].get(t, np.nan) for s, t in zip(samples.symbol[mask], samples.t[mask], strict=True)])
    y, times = samples.y[mask], samples.t[mask]
    ok = ~np.isnan(edge)
    out = {}
    for name, part in (("selection", times < split_time), ("holdout", times >= split_time)):
        keep = ok & part
        if keep.sum() < 50 or len(np.unique(y[keep])) < 2:
            continue
        out[name] = {
            "rows": int(keep.sum()),
            "base_rate": float(y[keep].mean()),
            "auc": float(roc_auc_score(y[keep], edge[keep])),
            "net_top_quintile": float(samples.net[mask][keep][edge[keep] >= np.quantile(edge[keep], 0.8)].mean()),
            "net_all": float(samples.net[mask][keep].mean()),
        }
    if ok.sum() >= 50:
        probability = np.clip(edge[ok] + y[ok].mean(), 0, 1)
        out["brier_vs_base"] = [
            float(brier_score_loss(y[ok], probability)),
            float(brier_score_loss(y[ok], np.full(ok.sum(), y[ok].mean()))),
        ]
        out["calibration"] = calibration_table(y[ok], edge[ok]).reset_index(drop=True).to_dict("records")
    return out


def run_ml_study(config: StudyConfig, data_dir: Path, models_dir: Path, out_dir: Path, processes: int | None = None):
    ml = config.raw["ml"]
    started = datetime.now(UTC)
    symbols = list(config.tradable) + [s.removesuffix("TMN") + "USDT" for s in config.tradable if s != "USDTTMN"]
    features = build_features(data_dir, symbols)
    frames = ml_frames(features, list(config.tradable), list(config.tradable))
    slippage = {symbol: float(value) for symbol, value in config.slippage.items()}
    spec = WalkForwardSpec(
        min_train_days=float(ml["min_train_days"]),
        block_days=float(ml["block_days"]),
        calibration_share=float(ml["calibration_share"]),
        sample_every=int(ml["sample_every"]),
    )
    trade_symbols = list(config.strategy["symbols"])
    warmup = int(config.windows.warmup_days * 288)

    starts = window_starts(pd.DatetimeIndex(next(iter(features.values())).index), config.windows)
    selection_all, holdout = split_selection_holdout(starts, config.windows, config.selection_share)
    split_time = holdout[0] if holdout else starts[-1]

    labels, samples_by_label, blocks = {}, {}, []
    for label in ml["labels"]:
        name = label["name"]
        barrier = BarrierSpec(stop=label["stop"], take_profit=label["take_profit"], horizon=int(label["horizon"]))
        samples = build_samples(
            features, frames, list(ml["train_symbols"]), barrier, slippage, spec.sample_every, warmup
        )
        edges, label_blocks = walk_forward(samples, {s: frames[s] for s in trade_symbols}, spec, barrier.horizon * BAR)
        for symbol in features:
            column = edges.get(symbol)
            features[symbol][f"ml_edge_{name}"] = column if column is not None else np.nan
        labels[name] = {"barrier": barrier.__dict__, "oof": _oof_metrics(samples, edges, trade_symbols, split_time)}
        samples_by_label[name] = (samples, barrier)
        blocks.append(label_blocks.assign(label=name))

    data = build_market_data(features, config.tradable, load_rules(data_dir))
    first_prediction = max(
        features[s][f"ml_edge_{label['name']}"].first_valid_index() for s in trade_symbols for label in ml["labels"]
    )
    selection = [start for start in selection_all if start >= first_prediction]

    rules = copy.deepcopy(config.strategy)
    rules["ml"] = {"enabled": False}
    variants = [("rules", rules)]
    for label in ml["labels"]:
        for threshold in ml["thresholds"]:
            for mode, sizing in (("filter", False), ("filter", True), ("tilt", False)):
                params = copy.deepcopy(config.strategy)
                params["ml"] = {
                    "enabled": True,
                    "column": f"ml_edge_{label['name']}",
                    "threshold": float(threshold),
                    "sizing": sizing,
                    "mode": mode,
                }
                suffix = " +size" if sizing else (" tilt" if mode == "tilt" else "")
                variants.append((f"{label['name']} t={threshold:+.2f}{suffix}", params))

    params_list = [params for _, params in variants]
    selection_rows = run_windows(
        data, build_strategy, params_list, {"base": config.costs()}, RiskLimits(), config.windows, selection, processes
    )
    table = []
    for index, (name, _) in enumerate(variants):
        summary = window_summary(selection_rows[selection_rows["params_id"] == index])
        score = objective(summary, float(ml["min_positive"]), float(ml["min_p10_drawdown"]), ml["objective_metric"])
        table.append({"variant": name, "params_id": index, "objective": score, **summary})
    ablation = pd.DataFrame(table)
    candidates = ablation[ablation["variant"] != "rules"]
    feasible = candidates[candidates["objective"] > float("-inf")]
    pool = feasible if not feasible.empty else candidates[candidates["p10_drawdown"] >= float(ml["min_p10_drawdown"])]
    pool = pool if not pool.empty else candidates
    chosen_id = int(pool.sort_values(ml["objective_metric"]).iloc[-1]["params_id"])
    chosen_name, chosen = variants[chosen_id]
    # Also report the best hard filter (entries below the threshold blocked), for the quality-vs-quantity view.
    hard = candidates[candidates["variant"].map(lambda name: "tilt" not in name and "+size" not in name)]
    hard_id = int(hard.sort_values(ml["objective_metric"]).iloc[-1]["params_id"])
    hard_name, hard_filter = variants[hard_id]

    # Control: a random filter that passes the same share of rule entries as the chosen ML filter.
    # If ML only "works" by trading less, the control matches it.
    pass_rate = _pass_rate(features, trade_symbols, chosen["ml"]["column"], chosen["ml"]["threshold"], first_prediction)
    controls = []
    for seed in range(RANDOM_CONTROLS):
        rng = np.random.default_rng(seed)
        for symbol in features:
            features[symbol][f"random_{seed}"] = rng.random(len(features[symbol]))
        control = copy.deepcopy(chosen)
        control["ml"] = {
            **chosen["ml"],
            "column": f"random_{seed}",
            "threshold": 1.0 - pass_rate,
            "sizing": False,
        }
        controls.append(control)
    data = build_market_data(features, config.tradable, load_rules(data_dir))

    final = run_windows(
        data,
        build_strategy,
        [rules, chosen, hard_filter, *controls],
        config.cost_cases(),
        RiskLimits(),
        config.windows,
        selection + holdout,
        processes,
    )
    holdout_set = {start.isoformat() for start in holdout}
    final["split"] = final["start"].map(lambda s: "holdout" if s in holdout_set else "selection")
    names = {0: "rules", 1: "rules+ml", 2: "rules+ml_hard_filter"}
    final["variant"] = final["params_id"].map(lambda i: names.get(i, "random_control"))
    comparison, per_trade = {}, {}
    for (variant, case, split), part in final.groupby(["variant", "cost_case", "split"]):
        key = f"{variant}/{split}/{case}"
        comparison[key] = window_summary(part)
        traded = part[part["trades"] > 0]
        per_trade[key] = {
            "trades": int(part["trades"].sum()),
            "return_per_trade": float(part["return"].sum() / max(1, part["trades"].sum())),
            "trade_win_rate": float((traded["win_rate"] * traded["trades"]).sum() / max(1, traded["trades"].sum())),
        }

    label_name = chosen["ml"]["column"].removeprefix("ml_edge_")
    samples, barrier = samples_by_label[label_name]
    final_model = fit(samples, barrier.horizon * BAR, spec.calibration_share)
    digest, payload = config_hash(config, chosen, _manifest(data_dir))
    model_meta = {
        "label": label_name,
        "barrier": barrier.__dict__,
        "train_symbols": ml["train_symbols"],
        "trained_until": samples.t_end.max().isoformat(),
        "walk_forward": spec.__dict__,
        "config_hash": digest,
        "data_manifest_sha256": _manifest_digest(data_dir),
        "created_at": started.isoformat(),
    }
    model_dir = models_dir / label_name
    model_id = save(final_model, model_dir, model_meta)
    importance = pd.DataFrame(
        {"feature": final_model.features, "gain": final_model.booster.feature_importance("gain")}
    ).sort_values("gain", ascending=False)

    out_dir.mkdir(parents=True, exist_ok=True)
    ablation.to_csv(out_dir / "ablation_selection.csv", index=False)
    final.to_csv(out_dir / "windows.csv", index=False)
    pd.concat(blocks).to_csv(out_dir / "walk_forward_blocks.csv", index=False)
    importance.to_csv(out_dir / "feature_importance.csv", index=False)
    summary = {
        "config_hash": digest,
        "chosen_variant": chosen_name,
        "chosen_feasible": not feasible.empty,
        "hard_filter_variant": hard_name,
        "model_id": model_id,
        "model_dir": str(model_dir),
        "first_prediction": first_prediction.isoformat(),
        "ml_pass_rate": pass_rate,
        "random_controls": RANDOM_CONTROLS,
        "selection_windows": len(selection),
        "holdout_windows": len(holdout),
        "labels": labels,
        "comparison": comparison,
        "per_trade": per_trade,
        "runtime_seconds": (datetime.now(UTC) - started).total_seconds(),
    }
    write_json(out_dir / "summary.json", summary)
    write_json(out_dir / "config.json", {"config_hash": digest, "config": payload})
    _chart(out_dir, comparison, chosen_name)
    return summary


def _chart(out_dir: Path, comparison: dict, chosen_name: str) -> None:
    figure, axes = plt.subplots(1, 2, figsize=(11, 4))
    for axis, metric, label in (
        (axes[0], "mean_return", "mean 3-day return, %"),
        (axes[1], "positive_share", "positive windows, %"),
    ):
        names, values, colors = [], [], []
        for split in ("selection", "holdout"):
            for case in ("base", "fee_stress"):
                for variant, color in (
                    ("rules", "#4C78A8"),
                    ("rules+ml", "#F58518"),
                    ("rules+ml_hard_filter", "#E45756"),
                    ("random_control", "#9D9D9D"),
                ):
                    entry = comparison.get(f"{variant}/{split}/{case}")
                    if entry:
                        names.append(f"{split}\n{case}\n{variant}")
                        values.append(entry[metric] * 100)
                        colors.append(color)
        axis.bar(range(len(values)), values, color=colors)
        axis.set_xticks(range(len(values)), names, fontsize=6)
        axis.axhline(0, color="black", linewidth=0.8)
        axis.set_ylabel(label)
    figure.suptitle(f"AI ablation: rules vs rules + ML ({chosen_name})")
    figure.tight_layout()
    figure.savefig(out_dir / "ablation.png", dpi=120)
    plt.close(figure)


def _manifest(data_dir: Path) -> dict:
    path = data_dir / "MANIFEST.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _manifest_digest(data_dir: Path) -> str | None:
    import hashlib

    path = data_dir / "MANIFEST.json"
    if not path.exists():
        return None
    files = json.loads(path.read_text(encoding="utf-8"))["files"]
    candles = {name: entry["sha256"] for name, entry in sorted(files.items()) if name.startswith("candles/")}
    return hashlib.sha256(json.dumps(candles, sort_keys=True).encode()).hexdigest()
