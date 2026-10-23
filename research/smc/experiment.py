"""SMC experiments (≤ 30 configs in total, all logged in reports/experiments.jsonl).

A  smc_long rule module: 8-config grid on the 180-day 3-day selection windows (measured fees, live risk limits),
   the current live config (a) as the reference; the chosen config + (a) on the holdout once (--final);
   the last 14 days with setups/day, trades/day, win rate, avg R, DD.
B  SMC features added to the h24 ML model: same purged walk-forward; IC change and SHAP; the new predictions in
   the live config (a) as an ML-only proxy.
"""

from __future__ import annotations

import itertools
import tomllib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.costs import CostModel, Fees
from backtest.engine import Backtester
from backtest.experiment import run_experiment
from backtest.metrics import compute_metrics
from ml.p3_experiments import _manifest
from strategy.composite import CompositeStrategy, FairValueParams, MLFilterParams
from strategy.factory import build_strategy
from strategy.modules.smc_long import SMCLongModule, SMCModuleParams
from strategy.smc import SMCParams

from .data import market, smc_features

MEASURED = Fees(Decimal("0.0025"), Decimal("0.0030"))  # GET /v1/account/fee, TMN markets, 2026-10-23
LIVE_REF = "live config (a), ML-only proxy"


def measured(config):
    cases = {
        "low": Fees(Decimal("0.0015"), Decimal("0.0020")),
        "base": MEASURED,
        "stress": MEASURED.stressed(Decimal("1.5")),
    }
    return replace(config, fees=MEASURED, fee_cases=cases)


def load_smc_config(root: Path) -> dict:
    return tomllib.loads((root / "config" / "smc.toml").read_text(encoding="utf-8"))


def feature_variants(smc: dict) -> dict[str, SMCParams]:
    """One SMC feature set per (pivot_n, zone) in the grid; tag = column suffix."""
    base = dict(smc["features"])
    out = {}
    for pivot, zone in itertools.product(smc["grid"]["pivot_n"], smc["grid"]["zone"]):
        out[f"_p{pivot}{zone[:3]}{'ob' if zone == 'fvg_or_ob' else ''}"] = SMCParams(
            **(base | {"pivot_n": pivot, "zone": zone})
        )
    return out


def smc_variants(smc: dict, tags: list[str]) -> list[tuple[str, dict]]:
    module = dict(smc["module"])
    module["symbols"] = tuple(module["symbols"])
    out = []
    for tag, timeout in itertools.product(tags, smc["grid"]["timeout_bars"]):
        out.append((f"smc_long{tag}_t{timeout}", {"smc": module | {"tag": tag, "timeout_bars": timeout}}))
    return out


def factory(params: dict, symbols: list[str]):
    if "smc" in params:
        values = dict(params["smc"])
        values["symbols"] = tuple(values["symbols"])
        module = SMCLongModule(SMCModuleParams(**values))
        chosen = [s for s in symbols if s in module.symbols]
        return CompositeStrategy([module], chosen, FairValueParams(enabled=False), MLFilterParams())
    return build_strategy(params, symbols)


def first_prediction(features: dict, symbols: list[str]) -> pd.Timestamp:
    return max(pd.Series(features[s]["ml_net_h24"]).first_valid_index() for s in symbols)


def build(config, root: Path):
    """Data with every SMC feature variant + the live columns, and the SMC config."""
    smc = load_smc_config(root)
    variants = feature_variants(smc)
    features, frames = smc_features(config, root / "data", ["BTCTMN", "ETHTMN"], variants)
    return smc, list(variants), features, frames, market(config, features, root / "data")


def window_study(config, data, features, variants, limits, out: Path, reports: Path, final: bool, name: str) -> dict:
    """The 180-day 3-day window study (selection; holdout too with `final`), measured fees, live limits."""
    return run_experiment(
        name,
        config,
        data,
        _manifest(Path(config.path).parent.parent / "data"),
        variants,
        out,
        reports,
        references=(LIVE_REF,),
        final=final,
        since=first_prediction(features, ["BTCTMN", "ETHTMN"]),
        factory=factory,
        processes=4,
        notes="SMC research; measured fees TMN 0.25/0.30; live risk limits (15%/45%, 50k min order)",
        limits=limits,
    )


def recent(config, limits, data, features, variants: list[tuple[str, dict]], days: int = 14) -> dict:
    """Last `days` days: setups/day, trades/day, win rate, avg R, DD, per fee row."""
    end = data.clock[-1]
    start = end - pd.Timedelta(days=days)
    out = {}
    for name, params in variants:
        setups = None
        if "smc" in params:
            tag = params["smc"]["tag"]
            setups = 0
            for symbol in params["smc"]["symbols"]:
                column = pd.Series(features[symbol][f"setup_id_smc15{tag}"])
                window = column[(column.index > start) & (column.index <= end)].dropna()
                setups += window.nunique()
        for row, fees in config.fee_cases.items():
            costs = CostModel(fees, config.slippage, config.default_slippage)
            result = Backtester(data, factory(params, data.symbols), costs, limits, Decimal("5000000")).run(start, end)
            m = compute_metrics(result)
            r = [t.r_multiple for t in result.trades if np.isfinite(t.r_multiple)]
            out[f"{name}/{row}"] = {
                "return": m["return"],
                "max_drawdown": m["max_drawdown"],
                "trades": m["trades"],
                "trades_per_day": m["trades_per_day"],
                "win_rate": m["win_rate"],
                "avg_r": float(np.mean(r)) if r else None,
                "setups_per_day": setups / days if setups is not None else None,
                "fees_paid": m["fees_paid"],
            }
    return {"start": str(start), "end": str(end), "results": out}
