"""RiskGuard: the final word on every entry. Pure; used identically by backtest, shadow and live.

Order of checks for an ENTER intent (the first failing check rejects, with its reason):
  1. market policy: allowlisted and not blocked (checked again right before the order is sent);
  2. account status: kill switch, daily loss block, end-of-challenge window, risk_mode OFF, halt;
  3. one position per symbol, no second pending entry (no averaging down);
  4. self-trade guard: no buy while one of our own sells rests on the same market
     (the executor must cancel it first; the decision says so);
  5. LLM adjustments can only reduce risk: size fraction <= the strategy's, stop only tighter,
     take-profit only closer and still above the fee floor; risk_mode DEFENSIVE halves the size;
  6. sizing caps (risk per trade, 20% per position, 60% total, minimum order, precision);
  7. liquidity buffer: equity after every open position hits its stop must stay above
     liquidity_limit × (1 + liquidity_buffer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from exchange.symbols import MarketPolicy, MarketPolicyError

from .limits import RiskLimits, ceil_to_decimals, floor_to_decimals, size_entry

RISK_MODES = ("NORMAL", "DEFENSIVE", "OFF")
DEFENSIVE_SIZE = Decimal("0.5")
MIN_TP_FRACTION = Decimal("0.007")  # a tightened take-profit must still clear the round-trip cost


@dataclass(frozen=True)
class SymbolRules:
    amount_decimals: int
    price_decimals: int
    min_notional: Decimal


@dataclass(frozen=True)
class AccountState:
    now: datetime
    equity: Decimal
    initial_equity: Decimal
    day_start_equity: Decimal
    exposure: Decimal  # open positions at market + pending entry notional
    open_symbols: frozenset[str] = frozenset()
    pending_symbols: frozenset[str] = frozenset()
    resting_sell_symbols: frozenset[str] = frozenset()
    minutes_to_end: float | None = None  # None: no end (open-ended run)
    daily_blocked: bool = False
    halted: bool = False
    risk_mode: str = "NORMAL"
    liquidity_limit: Decimal | None = None


@dataclass(frozen=True)
class GuardStatus:
    entries_allowed: bool
    flatten_required: bool
    trigger_kill: bool
    trigger_daily_block: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LLMAdjustment:
    """What an LLM agent may ask for. RiskGuard clamps every field; nothing here can add risk."""

    approve: bool = True
    size_fraction: float | None = None
    stop_price: float | None = None
    take_profit: float | None = None


@dataclass(frozen=True)
class EntryDecision:
    approved: bool
    quantity: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    limit: Decimal = Decimal(0)
    stop: Decimal = Decimal(0)
    take_profit: Decimal | None = None
    reasons: tuple[str, ...] = ()
    actions: tuple[str, ...] = field(default=())  # e.g. "cancel_resting_sell:BTCTMN"

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _decimal(value: float) -> Decimal:
    return Decimal(repr(float(value)))


class RiskGuard:
    def __init__(self, limits: RiskLimits, policy: MarketPolicy | None = None) -> None:
        self.limits = limits
        self.policy = policy

    def status(self, state: AccountState) -> GuardStatus:
        limits = self.limits
        reasons = []
        kill = not state.halted and state.equity / state.initial_equity - 1 <= limits.kill_switch
        daily = not state.daily_blocked and state.equity / state.day_start_equity - 1 <= limits.daily_loss_limit
        halted = state.halted or kill
        blocked = state.daily_blocked or daily
        near_end = state.minutes_to_end is not None and state.minutes_to_end <= limits.no_entry_before_end_minutes
        flatten = halted or (
            state.minutes_to_end is not None and state.minutes_to_end <= limits.flatten_before_end_minutes
        )
        if halted:
            reasons.append("kill switch: halted")
        if blocked:
            reasons.append("daily loss limit: no entries until the next Tehran day")
        if near_end:
            reasons.append(f"no new entries in the last {limits.no_entry_before_end_minutes} minutes")
        if state.risk_mode == "OFF":
            reasons.append("risk_mode OFF")
        entries = not (halted or blocked or near_end or state.risk_mode == "OFF")
        return GuardStatus(entries, flatten, kill, daily, tuple(reasons))

    def check_entry(
        self,
        intent,
        state: AccountState,
        rules: SymbolRules,
        llm: LLMAdjustment | None = None,
    ) -> EntryDecision:
        symbol = intent.symbol
        if self.policy is not None:
            try:
                self.policy.check(symbol)
            except MarketPolicyError as exc:
                return EntryDecision(False, reasons=(f"market policy: {exc}",))
        status = self.status(state)
        if not status.entries_allowed:
            return EntryDecision(False, reasons=status.reasons)
        if symbol in state.open_symbols or symbol in state.pending_symbols:
            return EntryDecision(False, reasons=("one position per symbol: already open or pending",))
        if symbol in state.resting_sell_symbols:
            return EntryDecision(
                False,
                reasons=("self-trade guard: our own sell rests on this market",),
                actions=(f"cancel_resting_sell:{symbol}",),
            )
        if state.risk_mode not in RISK_MODES:
            return EntryDecision(False, reasons=(f"unknown risk_mode {state.risk_mode!r}",))

        reasons = []
        fraction = _decimal(intent.size_fraction)
        stop_value, tp_value = intent.stop_price, intent.take_profit
        if llm is not None:
            if not llm.approve:
                return EntryDecision(False, reasons=("rejected by the LLM trade agent",))
            if llm.size_fraction is not None and _decimal(llm.size_fraction) < fraction:
                fraction = max(Decimal(0), _decimal(llm.size_fraction))
                reasons.append(f"LLM reduced size to {fraction}")
            if llm.stop_price is not None and stop_value < llm.stop_price < intent.limit_price:
                stop_value = llm.stop_price
                reasons.append("LLM tightened the stop")
            if llm.take_profit is not None and tp_value is not None:
                floor = intent.limit_price * (1 + float(MIN_TP_FRACTION))
                if floor <= llm.take_profit < tp_value:
                    tp_value = llm.take_profit
                    reasons.append("LLM brought the take-profit closer")
        if state.risk_mode == "DEFENSIVE":
            fraction *= DEFENSIVE_SIZE
            reasons.append("risk_mode DEFENSIVE: half size")

        limit = floor_to_decimals(_decimal(intent.limit_price), rules.price_decimals)
        stop = ceil_to_decimals(_decimal(stop_value), rules.price_decimals)
        take_profit = floor_to_decimals(_decimal(tp_value), rules.price_decimals) if tp_value else None
        decision = size_entry(
            equity=state.equity,
            exposure=state.exposure,
            entry_price=limit,
            stop_price=stop,
            size_fraction=fraction,
            amount_decimals=rules.amount_decimals,
            exchange_min_notional=rules.min_notional,
            limits=self.limits,
            position_cap=self.limits.position_cap(symbol),
        )
        if decision.quantity <= 0:
            return EntryDecision(False, reasons=(*reasons, decision.reason))
        if state.liquidity_limit is not None:
            worst = state.equity - (state.exposure + decision.notional) * self.limits.max_stop_fraction
            floor = state.liquidity_limit * (1 + self.limits.liquidity_buffer)
            if worst < floor:
                return EntryDecision(
                    False, reasons=(*reasons, f"liquidity buffer: worst case {worst:.0f} below {floor:.0f}")
                )
        return EntryDecision(
            True,
            quantity=decision.quantity,
            notional=decision.notional,
            limit=limit,
            stop=stop,
            take_profit=take_profit,
            reasons=(*reasons, decision.reason),
        )
