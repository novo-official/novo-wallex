"""AI activity module (user decision 2026-10-22, trend OFF in live): about 20 small ML+LLM round trips.

Every slot (`slot_minutes`), the module ranks BTCTMN / ETHTMN / USDTTMN by the ML model's E[net]
(h24, calibrated, net of costs) and proposes them in that order, all in one exclusive group: the engine
takes at most one approved entry per slot. Each candidate still needs the Regime Officer's risk_mode
!= OFF and the Trade Agent's approval (RiskGuard after that); a rejected top candidate lets the next
one be evaluated. Every rejection is logged with its reason.

Trade: maker limit at the best bid (timeout, one reprice, then cancel), resting maker take-profit at
+take_profit, stop at -stop vs the best bid, maximum hold `max_hold_bars`, then a limit-at-best exit
that becomes market after a timeout. Limits: `max_concurrent` positions, `max_per_day` entries per
Tehran day, `min_spacing_minutes` between entries.

Without an LLM layer (backtest proxy) the module still proposes the same candidates: that run is an
ML-only proxy and is labeled so; live requires the LLM layer (execution/runner.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from strategy.base import Context, Intent


@dataclass(frozen=True)
class AIActivityParams:
    column: str = "ml_net_h24"
    symbols: tuple[str, ...] = ("BTCTMN", "ETHTMN", "USDTTMN")
    slot_minutes: int = 60
    min_spacing_minutes: int = 45
    max_per_day: int = 12
    max_concurrent: int = 2
    size_fraction: float = 1.0  # of the risk layer's position cap; set by config to give 250k TMN
    take_profit: float = 0.008
    stop: float = 0.010
    max_hold_bars: int = 24  # 2 h of 5m bars
    entry_timeout_minutes: float = 3.0
    reprices: int = 1


class AIActivityModule:
    name = "ai_activity"

    def __init__(self, params: AIActivityParams | None = None) -> None:
        self.params = params or AIActivityParams()
        self.symbols = list(self.params.symbols)
        self._last_slot: pd.Timestamp | None = None
        self._last_entry: pd.Timestamp | None = None
        self._day = None
        self._today = 0

    def note_entry(self, now: pd.Timestamp) -> None:
        """Called by the engine when one of this module's entries is approved (spacing and quota count it)."""
        self._last_entry = now
        self._today += 1

    def candidates(self, ctx: Context, open_symbols: set[str], open_count: int) -> list[Intent]:
        p = self.params
        now = ctx.now
        day = now.tz_convert("Asia/Tehran").date()
        if day != self._day:
            self._day, self._today = day, 0
        if self._last_slot is not None and now - self._last_slot < pd.Timedelta(minutes=p.slot_minutes):
            return []
        if self._last_entry is not None and now - self._last_entry < pd.Timedelta(minutes=p.min_spacing_minutes):
            return []
        if self._today >= p.max_per_day or open_count >= p.max_concurrent:
            return []
        self._last_slot = now
        ranked = []
        for symbol in p.symbols:
            if symbol in open_symbols or symbol not in ctx.features or p.column not in ctx.features[symbol]:
                continue
            edge = ctx.value(symbol, p.column)
            if math.isfinite(edge) and ctx.value(symbol, "volume") >= 0:
                ranked.append((edge, symbol))
        ranked.sort(reverse=True)
        intents = []
        for rank, (edge, symbol) in enumerate(ranked, start=1):
            close = ctx.value(symbol, "close")
            limit = close * (1 - 0.0002)  # the engine prices live entries at the best bid
            intents.append(
                Intent(
                    kind="ENTER",
                    symbol=symbol,
                    module=self.name,
                    reason=(
                        f"AI activity slot: rank {rank}/{len(ranked)} by ML E[net] {edge * 100:+.3f}% "
                        f"(2h hold); TP +{p.take_profit:.1%}, stop -{p.stop:.1%}"
                    ),
                    signals={p.column: edge, "rank": rank, "candidates": len(ranked)},
                    limit_price=limit,
                    stop_price=limit * (1 - p.stop),
                    take_profit=limit * (1 + p.take_profit),
                    size_fraction=p.size_fraction,
                    timeout_bars=1,
                    group=f"{self.name}:{now.isoformat()}",
                    limit_at_bid=True,
                    entry_timeout_minutes=p.entry_timeout_minutes,
                    reprices=p.reprices,
                )
            )
        return intents

    def entry(self, ctx: Context, symbol: str) -> Intent | None:
        return None  # candidates come as a ranked group from `candidates`

    def manage(self, ctx: Context, symbol: str, position) -> list[Intent]:
        held = ctx.index - position.opened_index
        if held >= self.params.max_hold_bars:
            return [
                Intent(
                    kind="EXIT",
                    symbol=symbol,
                    module=self.name,
                    reason=f"max hold {held} bars (2 h)",
                    exit_type="maker",
                    timeout_bars=1,
                )
            ]
        return []
