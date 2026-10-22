"""Capital and risk limits. Pure functions, used identically by backtest, shadow and live.

These are the hard caps from CLAUDE.md §4. Phase 4 wraps them in RiskGuard,
which also has the final word over the LLM agents; nothing upstream can loosen them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal


@dataclass(frozen=True)
class RiskLimits:
    max_position_fraction: Decimal = Decimal("0.20")  # of equity, per position
    max_total_exposure: Decimal = Decimal("0.60")  # of equity, all positions + pending entries
    risk_per_trade: Decimal = Decimal("0.005")  # equity lost if the stop is hit (before fees)
    max_stop_fraction: Decimal = Decimal("0.02")  # stop at most 2% below entry
    min_order_tmn: Decimal = Decimal("500000")  # strategy floor; the exchange floor is 50,000 TMN
    daily_loss_limit: Decimal = Decimal("-0.03")  # no new entries until the next Tehran day
    kill_switch: Decimal = Decimal("-0.06")  # from the starting equity: flatten everything and halt
    no_entry_before_end_minutes: int = 120
    flatten_before_end_minutes: int = 60
    # Per-symbol position caps that replace max_position_fraction, e.g. (("USDTTMN", Decimal("0.40")),).
    # Empty by default: the 20% rule holds everywhere unless the user approves an override.
    position_caps: tuple[tuple[str, Decimal], ...] = ()

    def position_cap(self, symbol: str) -> Decimal:
        return dict(self.position_caps).get(symbol, self.max_position_fraction)


@dataclass(frozen=True)
class SizeDecision:
    notional: Decimal  # TMN to spend; 0 when rejected
    quantity: Decimal
    reason: str  # why this size, or why rejected


def floor_to_decimals(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def ceil_to_decimals(value: Decimal, decimals: int) -> Decimal:
    """Round up. Used for sell stops: a rounded stop may only be tighter than intended, never wider."""
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_UP)


def size_entry(
    *,
    equity: Decimal,
    exposure: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    size_fraction: Decimal,
    amount_decimals: int,
    exchange_min_notional: Decimal,
    limits: RiskLimits,
    position_cap: Decimal | None = None,
) -> SizeDecision:
    """Risk-based size, capped by position and total exposure limits, floored to the amount precision."""
    if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
        return SizeDecision(Decimal(0), Decimal(0), "rejected: stop must be below a positive entry price")
    stop_fraction = (entry_price - stop_price) / entry_price
    if stop_fraction > limits.max_stop_fraction:
        return SizeDecision(Decimal(0), Decimal(0), f"rejected: stop {stop_fraction:.4f} wider than the cap")
    fraction = min(max(size_fraction, Decimal(0)), Decimal(1))
    by_risk = equity * limits.risk_per_trade / stop_fraction
    by_position = equity * (limits.max_position_fraction if position_cap is None else position_cap)
    by_exposure = equity * limits.max_total_exposure - exposure
    notional = min(by_risk, by_position, by_exposure) * fraction
    binding = min(
        (("risk per trade", by_risk), ("position cap", by_position), ("exposure cap", by_exposure)),
        key=lambda item: item[1],
    )[0]
    quantity = floor_to_decimals(notional / entry_price, amount_decimals)
    notional = quantity * entry_price
    floor = max(limits.min_order_tmn, exchange_min_notional)
    if notional < floor:
        return SizeDecision(Decimal(0), Decimal(0), f"rejected: {notional:.0f} TMN below the minimum {floor:.0f}")
    return SizeDecision(notional, quantity, f"sized by {binding} x {fraction}")
