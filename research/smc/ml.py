"""Experiment B: SMC features added to the h24 E[net] model (same purged walk-forward, same labels and costs).

Reports the out-of-sample IC with and without the SMC features on the same rows (selection period only), SHAP of
a selection-period fit, and the new predictions in the live config (a) as an ML-only proxy (selection windows +
the last 14 days)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd

from backtest.experiments import log_experiment
from backtest.report import write_json
from ml.features import FEATURES
from ml.p3_experiments import selection_end
from ml.perbar import PerBarSpec, build_table, fit_net_model, oof_quality, shap_summary, walk_forward_net

from . import experiment as x
from .data import H24, ML_SYMBOLS, market, smc_columns
from .features import SMCParams

SMC_ML = {
    "fvg_dist_atr_smc15": "smc_fvg_dist_atr",
    "in_ob_smc15": "smc_in_ob",
    "bars_since_bos_smc15": "smc_bars_since_bos",
    "sweep_recent_smc15": "smc_sweep_recent",
    "pd_pct_smc15": "smc_pd_pct",
    "structure_smc15": "smc_structure_15m",
    "structure_smc1h": "smc_structure_1h",
}


def run_ml(config, root: Path, features, frames, data, live_params, limits, reports: Path) -> dict:
    smc = x.load_smc_config(root)
    params = SMCParams(**smc["features"])
    for symbol in ML_SYMBOLS:
        extra = smc_columns(root / "data", symbol, pd.DatetimeIndex(features[symbol].index), params, "_ml")
        for column, name in SMC_ML.items():
            frames[symbol][name] = extra[f"{column}_ml"].to_numpy()
    names = list(SMC_ML.values())
    slippage = {s: float(v) for s, v in config.slippage.items()}
    spec = PerBarSpec()
    until = selection_end(config, features)
    table = build_table(features, frames, ML_SYMBOLS, H24, slippage, spec, int(config.windows.warmup_days * 288))
    base_pred = {s: pd.Series(features[s]["ml_net_h24"]) for s in ML_SYMBOLS}
    smc_pred, _ = walk_forward_net(table, {s: frames[s] for s in ML_SYMBOLS}, FEATURES + names, H24, spec)
    quality = {"h24": oof_quality(table, base_pred, until), "h24 + SMC": oof_quality(table, smc_pred, until)}
    model = fit_net_model(table[table["t"] < until], FEATURES + names, H24, spec)
    shap = shap_summary(model, table.loc[table["t"] < until, FEATURES + names])
    shap["rank"] = range(1, len(shap) + 1)
    out = reports / "smc-ml"
    out.mkdir(parents=True, exist_ok=True)
    shap.to_csv(out / "shap_summary.csv", index=False)
    log_experiment(
        reports,
        "SMC B: SMC features in the h24 model (walk-forward IC, SHAP)",
        configs=1,
        holdout_evaluated=False,
        report="reports/smc-ml",
        notes="selection period only",
    )
    # the new predictions in the live config (a), ML-only proxy
    for symbol in features:
        features[symbol]["ml_net_h24smc"] = smc_pred[symbol] if symbol in smc_pred else float("nan")
    data = market(config, features, root / "data")
    smc_params = {**live_params, "ai_activity": {**live_params["ai_activity"], "column": "ml_net_h24smc"}}
    variants = [("live (a) with h24 + SMC features", smc_params), (x.LIVE_REF, live_params)]
    x.window_study(
        config,
        data,
        features,
        variants,
        limits,
        reports / "smc-ml-selection",
        reports,
        False,
        "SMC B: live (a) with h24+SMC vs h24, selection windows",
    )
    recent = x.recent(config, limits, data, features, variants)
    log_experiment(
        reports,
        "SMC B: live (a) with h24+SMC vs h24, last 14 days",
        configs=2,
        holdout_evaluated=False,
        report="reports/smc-ml",
        notes="measured fees; live limits",
    )
    summary = {
        "until": str(until),
        "quality": quality,
        "shap_smc": shap[shap["feature"].isin(names)].to_dict("records"),
        "shap_top10": shap.head(10).to_dict("records"),
        "recent": recent,
    }
    write_json(out / "summary.json", summary)
    print(summary)
    return summary


__all__ = ["run_ml", "replace"]
