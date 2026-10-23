"""`smc_long`: a long-only ICT setup module on the 5m clock (live from live-v2 by the team's decision; it failed the
research gate, docs/RESEARCH_SMC.md).

1h bullish structure -> a 15m setup (bullish sweep, then a displacement that breaks structure up and leaves an
FVG or OB) -> a maker LIMIT at the top of that zone on the retrace (timeout, then cancel). Stop under the sweep
low (at least `min_stop`, at most `max_stop`), take-profit at the nearest swing high above (buy-side liquidity),
required >= `min_rr` R and above the fee floor; only in discount (zone top in the lower half of the range
sweep low -> target)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.base import Context, Intent


@dataclass(frozen=True)
class SMCModuleParams:
    symbols: tuple[str, ...] = ("BTCTMN", "ETHTMN")
    size_fraction: float = 1 / 3  # of the 15% position cap: 250k TMN on 5M, like the live trades
    timeout_bars: int = 12  # 1 h of 5m bars for the retrace
    min_stop: float = 0.008
    max_stop: float = 0.02
    stop_buffer: float = 0.001
    min_rr: float = 1.5
    fee_floor: float = 0.007
    discount_max: float = 0.5
    max_hold_bars: int = 48  # 4 h
    require_htf: bool = True
    tag: str = ""  # which SMC feature variant (column suffix) to read


class SMCLongModule:
    name = "smc_long"

    def __init__(self, params: SMCModuleParams | None = None) -> None:
        self.params = params or SMCModuleParams()
        self.symbols = list(self.params.symbols)
        self.used: set[tuple[str, float]] = set()
        self.setups_seen: set[tuple[str, float]] = set()

    def entry(self, ctx: Context, symbol: str) -> Intent | None:
        p = self.params
        if symbol not in self.symbols:
            return None
        setup = ctx.value(symbol, f"setup_id_smc15{p.tag}")
        if not math.isfinite(setup):
            return None
        key = (symbol, setup)
        self.setups_seen.add(key)
        if key in self.used:
            return None
        if p.require_htf and ctx.value(symbol, f"structure_smc1h{p.tag}") != 1:
            return None
        top, sweep = ctx.value(symbol, f"setup_top_smc15{p.tag}"), ctx.value(symbol, f"setup_sweep_low_smc15{p.tag}")
        target, close = ctx.value(symbol, f"setup_tp_smc15{p.tag}"), ctx.value(symbol, "close")
        limit = min(top, close * (1 - 0.0002))  # maker: never above the market
        distance = (limit - sweep * (1 - p.stop_buffer)) / limit
        distance = min(max(distance, p.min_stop), p.max_stop)
        stop = limit * (1 - distance)
        reward, risk = target - limit, limit - stop
        if reward < p.min_rr * risk or reward / limit < p.fee_floor:
            return None
        if (limit - sweep) / (target - sweep) >= p.discount_max:
            return None
        self.used.add(key)
        return Intent(
            kind="ENTER",
            symbol=symbol,
            module=self.name,
            reason=(
                f"SMC: 1h bullish structure; 15m sweep of {sweep:.0f} then displacement + BOS; entry at the "
                f"zone top {limit:.0f} (discount), stop {distance:.2%}, target swing high {target:.0f} "
                f"({reward / risk:.1f}R)"
            ),
            signals={
                "smc_pattern": "1h bullish structure; 15m liquidity sweep below a swing low, then a displacement "
                "that broke structure up and left an FVG/OB; entry on the retrace into that zone",
                "smc_sweep_depth": (limit - sweep) / limit,
                "smc_target_distance": target / limit - 1,
                "smc_rr": reward / risk,
                "smc_discount": (limit - sweep) / (target - sweep),
                "smc_structure_1h": "bullish",
            },
            limit_price=limit,
            stop_price=stop,
            take_profit=target,
            size_fraction=p.size_fraction,
            timeout_bars=p.timeout_bars,
        )

    def manage(self, ctx: Context, symbol: str, position) -> list[Intent]:
        if ctx.index - position.opened_index >= self.params.max_hold_bars:
            return [
                Intent(
                    kind="EXIT",
                    symbol=symbol,
                    module=self.name,
                    reason=f"SMC max hold {self.params.max_hold_bars} bars",
                    exit_type="maker",
                    timeout_bars=1,
                )
            ]
        return []
