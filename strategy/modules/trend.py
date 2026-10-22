"""Trend module: 1h regime filter, 15m pullback setup, 5m breakout trigger. Long only.

Buy when
  * regime (1h): close > EMA200, EMA50 > EMA200 and EMA50 rising (uptrend);
  * setup (15m): RSI14 dipped below `setup_rsi` within the last hour (a pullback inside the uptrend);
  * trigger (5m): the bar traded (volume > 0) and closed above the highest high of the previous
    `breakout_bars` bars, and above the 15m EMA20 (the pullback is over).

Exits
  * stop: entry − `stop_atr` × ATR14(15m), the stop distance clipped to [min_stop, max_stop];
  * take-profit: entry + `tp_r` × risk, and at least `min_tp` above entry so it clears round-trip fees;
  * after +1R the stop moves to entry + `breakeven_buffer` (fees covered), then trails at
    highest close − `trail_atr` × ATR14(15m);
  * regime exit: the 1h close falls below the 1h EMA50;
  * time exit: after `max_hold_bars` bars without reaching +1R.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.base import Context, Intent


@dataclass(frozen=True)
class TrendParams:
    setup_rsi: float = 45.0
    breakout_bars: int = 12
    stop_atr: float = 2.0
    min_stop: float = 0.006
    max_stop: float = 0.02
    tp_r: float = 2.5
    min_tp: float = 0.012
    trail_atr: float = 2.5
    breakeven_buffer: float = 0.007
    max_hold_bars: int = 288
    entry_offset: float = 0.0005  # buy limit this far below the trigger close (rests on the bid)
    min_active_share: float = 0.3  # share of 5m bars with trades over the last day
    timeout_bars: int = 3
    require_4h: bool = False  # also require the 4h close above the 4h EMA50
    regime_exit: str = "1h"  # "1h": exit when the 1h close < 1h EMA50; "4h": same on 4h; "none"
    trigger: str = "pullback_breakout"  # or "breakout" (no pullback needed)


class TrendModule:
    name = "trend"

    def __init__(self, params: TrendParams | None = None) -> None:
        self.params = params or TrendParams()

    def regime_ok(self, ctx: Context, symbol: str) -> bool:
        close_1h = ctx.value(symbol, "close_1h")
        ema50, ema200 = ctx.value(symbol, "ema50_1h"), ctx.value(symbol, "ema200_1h")
        slope = ctx.value(symbol, "ema50_slope_1h")
        if not (close_1h > ema200 and ema50 > ema200 and slope > 0):
            return False
        return not self.params.require_4h or ctx.value(symbol, "close_4h") > ctx.value(symbol, "ema50_4h")

    def entry(self, ctx: Context, symbol: str) -> Intent | None:
        p = self.params
        if not self.regime_ok(ctx, symbol):
            return None
        if not ctx.value(symbol, "active_share_1d") >= p.min_active_share:
            return None
        recent_rsi = ctx.window(symbol, "rsi14_15m", 12)
        if recent_rsi.size == 0:
            return None
        if p.trigger == "pullback_breakout" and not (recent_rsi < p.setup_rsi).any():
            return None
        close = ctx.value(symbol, "close")
        prior_high = ctx.value(symbol, f"prior_high_{p.breakout_bars}")
        ema20_15m = ctx.value(symbol, "ema20_15m")
        if not (ctx.value(symbol, "volume") > 0 and close > prior_high and close > ema20_15m):
            return None
        atr = ctx.value(symbol, "atr14_15m")
        if not math.isfinite(atr) or atr <= 0:
            return None
        limit = close * (1 - p.entry_offset)
        stop_distance = min(max(p.stop_atr * atr / limit, p.min_stop), p.max_stop)
        stop = limit * (1 - stop_distance)
        take_profit = limit * (1 + max(p.tp_r * stop_distance, p.min_tp))
        return Intent(
            kind="ENTER",
            symbol=symbol,
            module=self.name,
            reason=(
                f"1h uptrend (close>EMA200, EMA50>EMA200, EMA50 rising){'; 4h close>EMA50' if p.require_4h else ''}"
                f"{f'; 15m pullback RSI<{p.setup_rsi:g} in the last hour' if p.trigger == 'pullback_breakout' else ''}"
                f"; 5m close {close:.6g} broke the {p.breakout_bars}-bar high {prior_high:.6g}"
            ),
            signals={
                "close": close,
                "prior_high": prior_high,
                "rsi14_15m_min_1h": float(recent_rsi.min()),
                "ema50_slope_1h": ctx.value(symbol, "ema50_slope_1h"),
                "atr14_15m": atr,
                "stop_distance": stop_distance,
            },
            limit_price=limit,
            stop_price=stop,
            take_profit=take_profit,
            timeout_bars=p.timeout_bars,
        )

    def manage(self, ctx: Context, symbol: str, position) -> list[Intent]:
        p = self.params
        entry = float(position.entry_price)
        risk = float(position.initial_risk)
        held = ctx.index - position.opened_index
        intents: list[Intent] = []
        if p.regime_exit != "none":
            suffix = "_" + p.regime_exit
            if ctx.value(symbol, "close" + suffix) < ctx.value(symbol, "ema50" + suffix):
                reason = f"regime exit: {p.regime_exit} close < {p.regime_exit} EMA50"
                return [Intent(kind="EXIT", symbol=symbol, module=self.name, reason=reason)]
        reached_1r = position.highest_close >= entry + risk
        if held >= p.max_hold_bars and not reached_1r:
            return [Intent(kind="EXIT", symbol=symbol, module=self.name, reason=f"time exit after {held} bars < +1R")]
        if reached_1r:
            atr = ctx.value(symbol, "atr14_15m")
            trail = position.highest_close - p.trail_atr * atr if math.isfinite(atr) else -math.inf
            new_stop = max(entry * (1 + p.breakeven_buffer), trail)
            if new_stop > float(position.stop_price):
                intents.append(
                    Intent(
                        kind="SET_STOP",
                        symbol=symbol,
                        module=self.name,
                        reason="trail after +1R",
                        stop_price=new_stop,
                    )
                )
        return intents
