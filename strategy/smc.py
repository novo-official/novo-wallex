"""ICT / Smart Money Concepts as quantified, causal features (the `smc_long` module; research: research/smc/).

Pure functions over ONE timeframe's closed bars (columns open/high/low/close, index = bar open time). Every value
at bar t uses bars <= t only. A swing point at bar i is known only from bar i + N (N right bars). Long only.

Definitions (STRATEGY.md "SMC research module"):
  swing high/low   fractal pivot: high[i] > the N highs before it and >= the N after it (lows mirrored)
  BOS up / down    a close above the last confirmed swing high (below the last swing low), once per level
  structure        +1 after a BOS up, -1 after a BOS down; CHoCH = the first BOS against the prior structure
  bullish sweep    a low under the last confirmed swing low (or a cluster of equal lows within 0.1%), and a
                   close back above that level within <= 2 bars
  displacement up  an up bar with body >= 1.5 ATR(14), or a k-bar rise (close - open k-1 bars back) >= 2 ATR
  bullish FVG      low[i] > high[i-2]; zone [high[i-2], low[i]]; size >= 0.3 ATR; valid until a close below
                   the zone bottom or max age
  bullish OB       the last down-close bar before a displacement that produced a BOS up; zone [low, open];
                   invalid on a close below its low
  premium/discount (close - last swing low) / (last swing high - last swing low); discount < 0.5
  setup            sweep -> within `setup_window` bars a displacement + BOS/CHoCH up -> the FVG (or OB) it left
                   is the entry zone; TP = the nearest confirmed swing high above (buy-side liquidity); armed
                   for `setup_max_age` bars, cancelled by a close below the zone or the sweep low
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategy.data import as_of, fill_gaps, resample
from strategy.features import atr as atr_series


@dataclass(frozen=True)
class SMCParams:
    pivot_n: int = 3
    atr_period: int = 14
    equal_lows_tolerance: float = 0.001
    sweep_close_back_bars: int = 2
    displacement_body_atr: float = 1.5
    displacement_move_atr: float = 2.0
    displacement_k: int = 3
    fvg_min_atr: float = 0.3
    fvg_max_age: int = 48
    ob_lookback: int = 10
    setup_window: int = 8
    setup_max_age: int = 16
    zone: str = "fvg"  # "fvg" or "fvg_or_ob"
    sweep_recent_bars: int = 8


COLUMNS = [
    "swing_high",
    "swing_low",
    "structure",
    "bos_up",
    "bos_down",
    "choch_up",
    "bars_since_bos",
    "sweep_bull",
    "sweep_low",
    "sweep_recent",
    "displacement_up",
    "fvg_new",
    "fvg_dist_atr",
    "in_ob",
    "pd_pct",
    "setup_id",
    "setup_top",
    "setup_bottom",
    "setup_sweep_low",
    "setup_tp",
]


def smc_frame(bars: pd.DataFrame, params: SMCParams | None = None) -> pd.DataFrame:
    """All SMC columns for one timeframe, causal at each bar's close."""
    p = params or SMCParams()
    o, h, lo, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    a = atr_series(bars, p.atr_period).to_numpy(dtype=float)
    n, size = p.pivot_n, len(bars)
    out = {k: np.full(size, np.nan) for k in COLUMNS}
    for k in ("bos_up", "bos_down", "choch_up", "sweep_bull", "displacement_up", "fvg_new", "in_ob"):
        out[k] = np.zeros(size, dtype=bool)
    highs: list[tuple[int, float]] = []  # confirmed swing highs (index, level)
    lows: list[tuple[int, float]] = []
    state, last_bos = 0, -1
    broken_high, broken_low, swept_low = -1, -1, -1
    pending = None  # (level, level_id, start, min_low)
    last_sweep = None  # (bar, sweep_low)
    fvgs: list[list] = []  # [top, bottom, created]
    obs: list[list] = []  # [top(open), bottom(low), created]
    setup = None
    for t in range(size):
        i = t - n  # the bar whose pivot status is known at t
        if i >= n:
            if h[i] > h[i - n : i].max() and h[i] >= h[i + 1 : t + 1].max():
                highs.append((i, h[i]))
            if lo[i] < lo[i - n : i].min() and lo[i] <= lo[i + 1 : t + 1].min():
                lows.append((i, lo[i]))
        atr_t = a[t]
        sh_id, sh = highs[-1] if highs else (-1, math.nan)
        sl_id, sl = lows[-1] if lows else (-1, math.nan)
        out["swing_high"][t], out["swing_low"][t] = sh, sl
        # displacement (needed by BOS/OB below)
        k0 = max(0, t - p.displacement_k + 1)
        disp = math.isfinite(atr_t) and (
            (c[t] > o[t] and c[t] - o[t] >= p.displacement_body_atr * atr_t)
            or (c[t] - o[k0] >= p.displacement_move_atr * atr_t)
        )
        out["displacement_up"][t] = disp
        # structure
        if sh_id >= 0 and c[t] > sh and sh_id != broken_high:
            out["bos_up"][t] = True
            out["choch_up"][t] = state == -1
            state, last_bos, broken_high = 1, t, sh_id
            recent_disp = any(out["displacement_up"][max(0, t - p.displacement_k + 1) : t + 1])
            if recent_disp:  # order block: the last down-close bar before the displacement
                start = t - p.displacement_k + 1
                for j in range(max(0, start) - 1, max(-1, start - 1 - p.ob_lookback), -1):
                    if c[j] < o[j]:
                        obs.append([o[j], lo[j], t])
                        break
        elif sl_id >= 0 and c[t] < sl and sl_id != broken_low:
            out["bos_down"][t] = True
            state, last_bos, broken_low = -1, t, sl_id
        out["structure"][t] = state
        out["bars_since_bos"][t] = t - last_bos if last_bos >= 0 else math.nan
        # bullish sweep of the last swing low (or a cluster of equal lows)
        if sl_id >= 0 and sl_id != swept_low:
            level = sl
            if len(lows) > 1 and abs(lows[-2][1] - sl) <= p.equal_lows_tolerance * sl:
                level = min(sl, lows[-2][1])
            if pending is None and lo[t] < level:
                pending = (level, sl_id, t, lo[t])
        if pending is not None:
            level, level_id, start, min_low = pending
            min_low = min(min_low, lo[t])
            if c[t] > level:
                out["sweep_bull"][t], out["sweep_low"][t] = True, min_low
                swept_low, last_sweep, pending = level_id, (t, min_low), None
            elif t - start >= p.sweep_close_back_bars:
                swept_low, pending = level_id, None
            else:
                pending = (level, level_id, start, min_low)
        out["sweep_recent"][t] = last_sweep is not None and t - last_sweep[0] <= p.sweep_recent_bars
        # FVGs
        if t >= 2 and math.isfinite(atr_t) and lo[t] > h[t - 2] and lo[t] - h[t - 2] >= p.fvg_min_atr * atr_t:
            fvgs.append([lo[t], h[t - 2], t])
            out["fvg_new"][t] = True
        fvgs = [z for z in fvgs if c[t] >= z[1] and t - z[2] <= p.fvg_max_age]
        obs = [z for z in obs if c[t] >= z[1]]
        below = [z for z in fvgs if z[1] <= c[t]]
        if below and math.isfinite(atr_t) and atr_t > 0:
            top = max(z[0] for z in below)
            out["fvg_dist_atr"][t] = max(0.0, c[t] - top) / atr_t
        out["in_ob"][t] = any(z[1] <= c[t] <= z[0] for z in obs)
        if sh_id >= 0 and sl_id >= 0 and sh > sl:
            out["pd_pct"][t] = (c[t] - sl) / (sh - sl)
        # setup state machine
        if setup is not None and (t - setup["t"] > p.setup_max_age or c[t] < setup["bottom"] or c[t] <= setup["sweep"]):
            setup = None
        if last_sweep is not None and out["bos_up"][t] and t - last_sweep[0] <= p.setup_window:
            if any(out["displacement_up"][max(0, t - p.displacement_k + 1) : t + 1]):
                zone = None
                fresh = [z for z in fvgs if z[2] >= last_sweep[0]]
                if fresh:
                    zone = fresh[-1][:2]
                elif p.zone == "fvg_or_ob" and obs and obs[-1][2] == t:
                    zone = obs[-1][:2]
                targets = [lvl for _, lvl in highs[-30:] if lvl > c[t]]
                if zone is not None and targets:
                    setup = {"t": t, "top": zone[0], "bottom": zone[1], "sweep": last_sweep[1], "tp": min(targets)}
                    last_sweep = None
        if setup is not None:
            out["setup_id"][t] = setup["t"]
            out["setup_top"][t], out["setup_bottom"][t] = setup["top"], setup["bottom"]
            out["setup_sweep_low"][t], out["setup_tp"][t] = setup["sweep"], setup["tp"]
    frame = pd.DataFrame(out, index=bars.index)
    if "close_time" in bars:
        frame["close_time"] = bars["close_time"].to_numpy()
    return frame


HTF_COLUMNS = ["structure", "bos_up", "bars_since_bos", "pd_pct"]


def smc_on_clock(bars_1m: pd.DataFrame, clock: pd.DatetimeIndex, params: SMCParams, tag: str = "") -> pd.DataFrame:
    """SMC columns on the strategy's 5m clock: the 15m setup/structure (suffix _smc15) and the 1h structure
    (_smc1h), as of the last *closed* higher-timeframe bar. The same function feeds the backtest and live."""
    bars = fill_gaps(bars_1m)
    f15, f60 = smc_frame(resample(bars, 15), params), smc_frame(resample(bars, 60), params)
    joined = as_of(f15, clock, COLUMNS, f"_smc15{tag}").join(as_of(f60, clock, HTF_COLUMNS, f"_smc1h{tag}"))
    for column in joined.columns:  # booleans arrive as objects after the as-of join
        if joined[column].dtype == object:
            joined[column] = joined[column].astype(float)
    return joined
