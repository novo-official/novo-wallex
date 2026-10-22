"""USDT regime allocation: hold USDTTMN while the USDTTMN regime is up (the Toman devaluation trend).

  * regime (closed bars only):
      "4h"     — 4h close > 4h EMA50 and 4h EMA20 > 4h EMA50;
      "1h200"  — 1h close > 1h EMA200 and the 1h EMA50 rising;
      "4h200"  — 4h close > 4h EMA200 (about a 33-day trend);
  * entry: maker limit at close × (1 − entry_offset), re-tried while the regime stays up;
  * stop: `stop_atr` × ATR14(1h) below entry, clipped to [min_stop, max_stop] (max 2%, the global cap);
  * exit: regime down (taker), the stop, or the end-of-window flatten (the engine returns everything
    to TMN before the end). After an exit, `cooldown_bars` must pass before re-entering.
The size is set by the risk layer: its per-symbol cap for USDTTMN (20% by default; 40% only as an
experiment) and the risk-per-trade rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.base import Context, Intent


@dataclass(frozen=True)
class USDTAllocationParams:
    symbol: str = "USDTTMN"
    regime: str = "4h"
    stop_atr: float = 3.0
    min_stop: float = 0.006
    max_stop: float = 0.02
    entry_offset: float = 0.0002
    timeout_bars: int = 6
    cooldown_bars: int = 12


class USDTAllocationModule:
    name = "usdt_allocation"

    def __init__(self, params: USDTAllocationParams | None = None) -> None:
        self.params = params or USDTAllocationParams()
        self.symbols = [self.params.symbol]
        self._last_exit_index: int | None = None
        self._held = False  # set while a position is managed; lets entry() notice a stop-out

    def regime_up(self, ctx: Context, symbol: str) -> bool:
        regime = self.params.regime
        if regime == "4h":
            return ctx.value(symbol, "close_4h") > ctx.value(symbol, "ema50_4h") and ctx.value(
                symbol, "ema20_4h"
            ) > ctx.value(symbol, "ema50_4h")
        if regime == "1h200":
            return (
                ctx.value(symbol, "close_1h") > ctx.value(symbol, "ema200_1h")
                and ctx.value(symbol, "ema50_slope_1h") > 0
            )
        if regime == "4h200":
            return ctx.value(symbol, "close_4h") > ctx.value(symbol, "ema200_4h")
        raise ValueError(f"unknown regime {regime!r}")

    def entry(self, ctx: Context, symbol: str) -> Intent | None:
        p = self.params
        if symbol != p.symbol:
            return None
        if self._held:  # the position is gone without a regime exit: it was stopped out
            self._held = False
            self._last_exit_index = ctx.index
        if not self.regime_up(ctx, symbol):
            return None
        if self._last_exit_index is not None and ctx.index - self._last_exit_index < p.cooldown_bars:
            return None
        atr = ctx.value(symbol, "atr14_1h")
        close = ctx.value(symbol, "close")
        if not (math.isfinite(atr) and atr > 0 and close > 0):
            return None
        limit = close * (1 - p.entry_offset)
        stop_distance = min(max(p.stop_atr * atr / limit, p.min_stop), p.max_stop)
        return Intent(
            kind="ENTER",
            symbol=symbol,
            module=self.name,
            reason=(
                f"USDTTMN regime '{p.regime}' up: hold USDT (stop {stop_distance * 100:.2f}% = {p.stop_atr:g} x 1h ATR)"
            ),
            signals={"close": close, "atr14_1h": atr, "stop_distance": stop_distance},
            limit_price=limit,
            stop_price=limit * (1 - stop_distance),
            timeout_bars=p.timeout_bars,
        )

    def manage(self, ctx: Context, symbol: str, position) -> list[Intent]:
        self._held = True
        if not self.regime_up(ctx, symbol):
            self._held = False
            self._last_exit_index = ctx.index
            return [
                Intent(
                    kind="EXIT", symbol=symbol, module=self.name, reason=f"USDTTMN regime '{self.params.regime}' down"
                )
            ]
        return []
