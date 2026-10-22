"""ML-only module: enter when the model's calibrated expected net return is above a threshold.

The column `ml_net_<target>` holds E[net return of the target trade] from the walk-forward model
(ml/perbar.py), already net of both fees and slippage. The module trades exactly that trade:
  * entry: maker limit at close × (1 − entry_offset) with a timeout;
  * exit: after `hold_bars` (time exit), or the stop; for a barrier target also its take-profit;
  * size: from min_fraction at the threshold to 1.0 at threshold + full_size_margin (confidence),
    always inside the risk layer's caps.
A missing prediction (no model yet) means no entry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.base import Context, Intent


@dataclass(frozen=True)
class MLSignalParams:
    column: str = "ml_net_h48"
    threshold: float = 0.0
    full_size_margin: float = 0.003
    min_fraction: float = 0.55
    size_scale: float = 1.0  # multiplies the final size; < 1 for small "activity" trades (needs a lower min order)
    hold_bars: int = 48
    stop: float = 0.015
    take_profit: float = 0.0  # 0: none (horizon targets); > 0 for a barrier target
    entry_offset: float = 0.0005
    timeout_bars: int = 2
    exit_type: str = "taker"
    max_entries_per_day: int = 0  # 0 = no quota; > 0 caps entry intents per Tehran day (shadow-only variant)


class MLSignalModule:
    name = "ml_signal"

    def __init__(self, params: MLSignalParams | None = None) -> None:
        self.params = params or MLSignalParams()
        self._day = None
        self._entries_today = 0

    def entry(self, ctx: Context, symbol: str) -> Intent | None:
        p = self.params
        if p.column not in ctx.features[symbol]:
            return None
        edge = ctx.value(symbol, p.column)
        if not math.isfinite(edge) or edge <= p.threshold or not ctx.value(symbol, "volume") > 0:
            return None
        if p.max_entries_per_day > 0:
            day = ctx.now.tz_convert("Asia/Tehran").date()
            if day != self._day:
                self._day, self._entries_today = day, 0
            if self._entries_today >= p.max_entries_per_day:
                return None
            self._entries_today += 1  # counts intents; an unfilled entry still uses the quota
        close = ctx.value(symbol, "close")
        limit = close * (1 - p.entry_offset)
        confidence = min(1.0, (edge - p.threshold) / p.full_size_margin)
        return Intent(
            kind="ENTER",
            symbol=symbol,
            module=self.name,
            reason=f"ML expected net return {edge * 100:+.3f}% > {p.threshold * 100:+.3f}% over {p.hold_bars} bars",
            signals={p.column: edge, "close": close},
            limit_price=limit,
            stop_price=limit * (1 - p.stop),
            take_profit=limit * (1 + p.take_profit) if p.take_profit > 0 else None,
            size_fraction=(p.min_fraction + (1 - p.min_fraction) * confidence) * p.size_scale,
            timeout_bars=p.timeout_bars,
        )

    def manage(self, ctx: Context, symbol: str, position) -> list[Intent]:
        held = ctx.index - position.opened_index
        if held >= self.params.hold_bars:
            return [
                Intent(
                    kind="EXIT",
                    symbol=symbol,
                    module=self.name,
                    reason=f"time exit after {held} bars",
                    exit_type=self.params.exit_type,
                )
            ]
        return []
