"""Mean-reversion module: only in a range regime. Long only.

Buy when
  * regime (1h): ADX14 < `max_adx` (no trend) and the 1h close is above the 1h EMA200
    (do not catch a falling knife in a downtrend);
  * the 15m close is below the lower Bollinger band (20, 2σ) and RSI14(15m) < `max_rsi`;
  * the 5m bar traded.
Exit at the 15m Bollinger middle band (take-profit, which must clear `min_tp`), a stop
at `stop_atr` × ATR14(15m) clipped to [min_stop, max_stop], or after `max_hold_bars`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.base import Context, Intent


@dataclass(frozen=True)
class MeanReversionParams:
    max_adx: float = 20.0
    max_rsi: float = 30.0
    stop_atr: float = 1.5
    min_stop: float = 0.006
    max_stop: float = 0.02
    min_tp: float = 0.012
    max_hold_bars: int = 96
    entry_offset: float = 0.0005
    min_active_share: float = 0.3
    timeout_bars: int = 3


class MeanReversionModule:
    name = "mean_reversion"

    def __init__(self, params: MeanReversionParams | None = None) -> None:
        self.params = params or MeanReversionParams()

    def entry(self, ctx: Context, symbol: str) -> Intent | None:
        p = self.params
        adx = ctx.value(symbol, "adx14_1h")
        if not (adx < p.max_adx and ctx.value(symbol, "close_1h") > ctx.value(symbol, "ema200_1h")):
            return None
        if not ctx.value(symbol, "active_share_1d") >= p.min_active_share:
            return None
        close_15m, low_band = ctx.value(symbol, "close_15m"), ctx.value(symbol, "bb_low_15m")
        rsi = ctx.value(symbol, "rsi14_15m")
        if not (close_15m < low_band and rsi < p.max_rsi and ctx.value(symbol, "volume") > 0):
            return None
        atr = ctx.value(symbol, "atr14_15m")
        if not math.isfinite(atr) or atr <= 0:
            return None
        close = ctx.value(symbol, "close")
        limit = close * (1 - p.entry_offset)
        target = ctx.value(symbol, "bb_mid_15m")
        if not target / limit - 1 >= p.min_tp:
            return None  # the band is too narrow to pay the fees
        stop_distance = min(max(p.stop_atr * atr / limit, p.min_stop), p.max_stop)
        return Intent(
            kind="ENTER",
            symbol=symbol,
            module=self.name,
            reason=(
                f"1h range (ADX {adx:.1f} < {p.max_adx:g}, above EMA200); 15m close below the lower Bollinger band "
                f"with RSI {rsi:.1f} < {p.max_rsi:g}; target the 15m middle band {target:.6g}"
            ),
            signals={"adx14_1h": adx, "rsi14_15m": rsi, "bb_low_15m": low_band, "bb_mid_15m": target},
            limit_price=limit,
            stop_price=limit * (1 - stop_distance),
            take_profit=target,
            timeout_bars=p.timeout_bars,
        )

    def manage(self, ctx: Context, symbol: str, position) -> list[Intent]:
        held = ctx.index - position.opened_index
        if held >= self.params.max_hold_bars:
            return [Intent(kind="EXIT", symbol=symbol, module=self.name, reason=f"time exit after {held} bars")]
        return []
