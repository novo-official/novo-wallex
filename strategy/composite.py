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
class MLFilterParams:
    """Entry filter and sizing from the ML model (column `ml_edge`: calibrated P(win) minus the
    model's training base rate, so 0 means "no better than average").

    A missing value (no model trained yet for that time) blocks the entry: fail closed.
    """

    enabled: bool = False
    threshold: float = 0.05
    column: str = "ml_edge"
    mode: str = "filter"  # "filter": block entries below the threshold; "tilt": take them at low_fraction
    low_fraction: float = 0.55  # x the 1M position cap: just above the 500k minimum after rounding
    sizing: bool = False  # scale size from min_fraction at the threshold to 1.0 at threshold + full_size_margin
    min_fraction: float = 0.5
    full_size_margin: float = 0.2


@dataclass(frozen=True)
class FairValueParams:
    enabled: bool = True
    max_z: float = 1.5
    discount_z: float = -1.0
    base_fraction: float = 0.8
    discount_fraction: float = 1.0


class CompositeStrategy:
    name = "composite"

    def __init__(
        self,
        modules: list,
        symbols: list[str],
        fair_value: FairValueParams | None = None,
        ml: MLFilterParams | None = None,
    ) -> None:
        self.modules = modules
        self.by_name = {module.name: module for module in modules}
        self.symbols = symbols
        self.fair_value = fair_value or FairValueParams(enabled=False)
        self.ml = ml or MLFilterParams()

    def notify_entry(self, intent: Intent, now) -> None:
        module = self.by_name.get(intent.module)
        if module is not None and hasattr(module, "note_entry"):
            module.note_entry(now)

    def _apply_ml(self, ctx: Context, intent: Intent) -> Intent | None:
        ml = self.ml
        if not ml.enabled:
            return intent
        column = ml.column
        probability = ctx.value(intent.symbol, column) if column in ctx.features[intent.symbol] else math.nan
        if not math.isfinite(probability):
            return None
        if probability < ml.threshold:
            if ml.mode != "tilt":
                return None
            return replace(
                intent,
                size_fraction=intent.size_fraction * ml.low_fraction,
                signals={**intent.signals, column: probability},
                reason=intent.reason + f"; ML {column}={probability:+.3f} < {ml.threshold:+.3f}: reduced size",
            )
        fraction = intent.size_fraction
        if ml.sizing:
            scale = min(1.0, max(0.0, (probability - ml.threshold) / ml.full_size_margin))
            fraction *= ml.min_fraction + (1.0 - ml.min_fraction) * scale
        return replace(
            intent,
            size_fraction=fraction,
            signals={**intent.signals, column: probability},
            reason=intent.reason + f"; ML {column}={probability:+.3f} >= {ml.threshold:+.3f}",
        )

    def _apply_fair_value(self, ctx: Context, intent: Intent) -> Intent | None:
        """Scales the module's own size (never replaces it): base_fraction, or discount_fraction at a discount."""
        fv = self.fair_value
        if not fv.enabled:
            return intent
        if "fv_valid" not in ctx.features[intent.symbol]:
            return replace(intent, size_fraction=intent.size_fraction * fv.base_fraction)
        valid = bool(ctx.features[intent.symbol]["fv_valid"][ctx.index])
        z = ctx.value(intent.symbol, "fv_z")
        if not valid or not math.isfinite(z):
            return replace(intent, size_fraction=intent.size_fraction * fv.base_fraction)
        signals = {**intent.signals, "fv_z": z, "fv_premium": ctx.value(intent.symbol, "fv_premium")}
        if z > fv.max_z:
            return None
        if z < fv.discount_z:
            return replace(
                intent,
                size_fraction=intent.size_fraction * fv.discount_fraction,
                signals=signals,
                reason=intent.reason + f"; fair-value discount z={z:.2f}: full size",
            )
        return replace(intent, size_fraction=intent.size_fraction * fv.base_fraction, signals=signals)

    def on_cycle(self, ctx: Context, portfolio: PortfolioView) -> list[Intent]:
        intents: list[Intent] = []
        for symbol, position in portfolio.positions.items():
            module = self.by_name.get(position.module)
            if module is not None:
                intents.extend(module.manage(ctx, symbol, position))
        if not portfolio.entries_allowed:
            return intents
        busy = set(portfolio.positions) | set(portfolio.pending_entries)
        for module in self.modules:
            if hasattr(module, "candidates"):  # ranked, exclusive-group proposals (AI activity)
                count = sum(1 for p in portfolio.positions.values() if p.module == module.name)
                intents.extend(module.candidates(ctx, busy, count))
        for symbol in self.symbols:
            if symbol in portfolio.positions or symbol in portfolio.pending_entries:
                continue
            for module in self.modules:
                allowed = getattr(module, "symbols", None)
                if allowed is not None and symbol not in allowed:
                    continue
                intent = module.entry(ctx, symbol)
                if intent is None:
                    continue
                intent = self._apply_fair_value(ctx, intent)
                if intent is not None:
                    intent = self._apply_ml(ctx, intent)
                if intent is not None:
                    intents.append(intent)
                break
        return intents
