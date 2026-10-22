"""Composite strategy: entry modules in priority order, plus the fair-value filter.

Fair value (Iran-specific): fv_TMN = price_USDT_pair × USDTTMN. The premium of the TMN
market over that value is z-scored over one day. When both legs are liquid enough
(`fv_valid`), an entry at a premium above `max_z` is skipped and an entry at a
discount below `discount_z` gets the full size; otherwise it gets `base_fraction`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from strategy.base import Context, Intent, PortfolioView


@dataclass(frozen=True)
class FairValueParams:
    enabled: bool = True
    max_z: float = 1.5
    discount_z: float = -1.0
    base_fraction: float = 0.8
    discount_fraction: float = 1.0


class CompositeStrategy:
    name = "composite"

    def __init__(self, modules: list, symbols: list[str], fair_value: FairValueParams | None = None) -> None:
        self.modules = modules
        self.by_name = {module.name: module for module in modules}
        self.symbols = symbols
        self.fair_value = fair_value or FairValueParams(enabled=False)

    def _apply_fair_value(self, ctx: Context, intent: Intent) -> Intent | None:
        fv = self.fair_value
        if not fv.enabled or "fv_valid" not in ctx.features[intent.symbol]:
            return replace(intent, size_fraction=fv.base_fraction if fv.enabled else 1.0)
        valid = bool(ctx.features[intent.symbol]["fv_valid"][ctx.index])
        z = ctx.value(intent.symbol, "fv_z")
        if not valid or not math.isfinite(z):
            return replace(intent, size_fraction=fv.base_fraction)
        signals = {**intent.signals, "fv_z": z, "fv_premium": ctx.value(intent.symbol, "fv_premium")}
        if z > fv.max_z:
            return None
        if z < fv.discount_z:
            return replace(
                intent,
                size_fraction=fv.discount_fraction,
                signals=signals,
                reason=intent.reason + f"; fair-value discount z={z:.2f}: full size",
            )
        return replace(intent, size_fraction=fv.base_fraction, signals=signals)

    def on_cycle(self, ctx: Context, portfolio: PortfolioView) -> list[Intent]:
        intents: list[Intent] = []
        for symbol, position in portfolio.positions.items():
            module = self.by_name.get(position.module)
            if module is not None:
                intents.extend(module.manage(ctx, symbol, position))
        if not portfolio.entries_allowed:
            return intents
        for symbol in self.symbols:
            if symbol in portfolio.positions or symbol in portfolio.pending_entries:
                continue
            for module in self.modules:
                intent = module.entry(ctx, symbol)
                if intent is None:
                    continue
                intent = self._apply_fair_value(ctx, intent)
                if intent is not None:
                    intents.append(intent)
                break
        return intents
