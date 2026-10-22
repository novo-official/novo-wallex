"""Builds a strategy from a plain parameter dict, so configs, grids and reports share one format."""

from __future__ import annotations

from dataclasses import asdict, fields

from .composite import CompositeStrategy, FairValueParams
from .modules.mean_reversion import MeanReversionModule, MeanReversionParams
from .modules.trend import TrendModule, TrendParams


def _subset(cls, values: dict) -> dict:
    names = {field.name for field in fields(cls)}
    unknown = set(values) - names
    if unknown:
        raise ValueError(f"unknown {cls.__name__} parameters: {sorted(unknown)}")
    return values


def build_strategy(params: dict, symbols: list[str]) -> CompositeStrategy:
    """params = {"modules": [...], "symbols": [...] or None, "trend": {...}, "mean_reversion": {...},
    "fair_value": {...}}"""
    modules = []
    for name in params.get("modules", ["trend"]):
        if name == "trend":
            modules.append(TrendModule(TrendParams(**_subset(TrendParams, params.get("trend", {})))))
        elif name == "mean_reversion":
            values = _subset(MeanReversionParams, params.get("mean_reversion", {}))
            modules.append(MeanReversionModule(MeanReversionParams(**values)))
        else:
            raise ValueError(f"unknown module {name!r}")
    fair_value = FairValueParams(**_subset(FairValueParams, params.get("fair_value", {"enabled": False})))
    allowed = params.get("symbols")
    chosen = [symbol for symbol in symbols if allowed is None or symbol in allowed]
    return CompositeStrategy(modules, chosen, fair_value)


def describe(params: dict) -> dict:
    """Fully expanded parameters (defaults filled in), for reports and the config hash."""
    strategy = build_strategy(params, params.get("symbols") or [])
    out = {"modules": [module.name for module in strategy.modules], "symbols": params.get("symbols")}
    for module in strategy.modules:
        out[module.name] = asdict(module.params)
    out["fair_value"] = asdict(strategy.fair_value)
    return out
