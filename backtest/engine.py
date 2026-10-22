"""Event-driven, multi-symbol backtester with a TMN portfolio.

The clock ticks on 5m bar closes. At each tick:
  1. orders placed at the previous tick are matched against this bar
     (market exits at the open, then stops, then take-profits, then entry limits);
  2. equity is marked at liquidation value (close − taker slippage − taker fee);
  3. loss limits and end-of-window rules are applied;
  4. the strategy proposes intents for the closed bar, and the risk layer sizes them.

Conservative fill rules:
  * a buy/sell limit fills only if the bar trades *through* it (low < limit / high > limit),
    at the limit price, maker fee;
  * stops fill at min(stop, open) minus taker slippage, taker fee;
  * when stop and take-profit are both inside one bar, the stop wins;
  * an entry that fills in a bar whose low reaches the stop is stopped out in that bar;
  * zero-volume bars (o = h = l = c) cannot fill a limit order;
  * every window ends flat in TMN: no new entries in the last 2 h, all positions closed
    with taker fills from 60 min before the end, and anything left is closed at the last bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from risk.limits import RiskLimits, floor_to_decimals, size_entry
from strategy.base import Context, FeatureFrame, Intent, PortfolioView, PositionView, Strategy

from .costs import CostModel

TEHRAN = ZoneInfo("Asia/Tehran")
ZERO = Decimal(0)


def to_decimal(value: float) -> Decimal:
    return Decimal(repr(float(value)))


@dataclass(frozen=True)
class SymbolRules:
    amount_decimals: int
    price_decimals: int
    min_notional: Decimal


@dataclass
class MarketData:
    clock: pd.DatetimeIndex
    features: dict[str, FeatureFrame]
    rules: dict[str, SymbolRules]
    symbols: list[str]  # tradable symbols, in priority order


@dataclass
class Position:
    symbol: str
    module: str
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    take_profit: Decimal | None
    initial_risk: Decimal
    opened_index: int
    entry_fee: Decimal
    highest_close: float
    decision_id: int


@dataclass
class PendingEntry:
    intent: Intent
    quantity: Decimal
    limit: Decimal
    stop: Decimal
    take_profit: Decimal | None
    expires_index: int
    decision_id: int


@dataclass
class Trade:
    symbol: str
    module: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    fees: Decimal
    pnl: Decimal  # after fees
    exit_reason: str
    entry_decision: int
    r_multiple: float


@dataclass
class BacktestResult:
    start: pd.Timestamp
    end: pd.Timestamp
    initial_equity: Decimal
    final_equity: Decimal
    equity: pd.Series  # float TMN at every tick
    trades: list[Trade]
    decisions: list[dict]
    fees_paid: Decimal
    exposure: pd.Series  # share of equity in positions at every tick
    final_tmn_share: Decimal
    halted: bool = False
    notes: list[str] = field(default_factory=list)


class Backtester:
    def __init__(
        self,
        data: MarketData,
        strategy: Strategy,
        costs: CostModel,
        limits: RiskLimits,
        initial_equity: Decimal = Decimal("5000000"),
        record_decisions: bool = True,
    ) -> None:
        self.data = data
        self.strategy = strategy
        self.costs = costs
        self.limits = limits
        self.initial_equity = initial_equity
        self.record_decisions = record_decisions

    def run(self, start: pd.Timestamp, end: pd.Timestamp) -> BacktestResult:
        clock = self.data.clock
        first = int(clock.searchsorted(start, side="left"))
        last = int(clock.searchsorted(end, side="right")) - 1
        if last <= first:
            raise ValueError(f"no bars between {start} and {end}")
        ctx = Context(clock, self.data.features, self.data.symbols)
        fees = self.costs.fees

        cash = self.initial_equity
        positions: dict[str, Position] = {}
        pending: dict[str, PendingEntry] = {}
        exits: dict[str, str] = {}  # symbol -> reason, filled at the next bar's open
        trades: list[Trade] = []
        decisions: list[dict] = []
        equity_curve = np.empty(last - first + 1)
        exposure_curve = np.empty(last - first + 1)
        fees_paid = ZERO
        halted = False
        day_key = None
        day_start_equity = self.initial_equity
        blocked_day = None
        notes: list[str] = []

        def price(symbol: str, column: str, index: int) -> float:
            return float(self.data.features[symbol][column][index])

        def close_position(symbol: str, fill: Decimal, fee_rate: Decimal, reason: str, index: int) -> None:
            nonlocal cash, fees_paid
            position = positions.pop(symbol)
            proceeds = position.quantity * fill
            fee = proceeds * fee_rate
            cash += proceeds - fee
            fees_paid += fee
            pnl = proceeds - fee - position.quantity * position.entry_price - position.entry_fee
            risk = position.initial_risk * position.quantity
            trades.append(
                Trade(
                    symbol=symbol,
                    module=position.module,
                    entry_time=clock[position.opened_index],
                    exit_time=clock[index],
                    entry_price=position.entry_price,
                    exit_price=fill,
                    quantity=position.quantity,
                    fees=position.entry_fee + fee,
                    pnl=pnl,
                    exit_reason=reason,
                    entry_decision=position.decision_id,
                    r_multiple=float(pnl / risk) if risk > 0 else 0.0,
                )
            )

        def log(index: int, symbol: str, action: str, reason: str, intent: Intent | None = None, risk: str = ""):
            if not self.record_decisions:
                return len(decisions)
            decisions.append(
                {
                    "id": len(decisions),
                    "time": clock[index].isoformat(),
                    "symbol": symbol,
                    "module": intent.module if intent else "risk",
                    "action": action,
                    "reason": reason,
                    "signals": dict(intent.signals) if intent else {},
                    "risk": risk,
                }
            )
            return len(decisions) - 1

        for step, index in enumerate(range(first, last + 1)):
            ctx.index = index
            now = clock[index]

            # 1. Match orders placed at the previous tick against bar `index`.
            for symbol, reason in list(exits.items()):
                if symbol in positions:
                    slip = self.costs.slippage_for(symbol)
                    fill = to_decimal(price(symbol, "open", index)) * (1 - slip)
                    close_position(symbol, fill, fees.taker, reason, index)
            exits.clear()

            for symbol in list(positions):
                position = positions[symbol]
                low, high, open_ = (
                    price(symbol, "low", index),
                    price(symbol, "high", index),
                    price(symbol, "open", index),
                )
                if low <= float(position.stop_price):
                    slip = self.costs.slippage_for(symbol)
                    fill = min(position.stop_price, to_decimal(open_)) * (1 - slip)
                    close_position(symbol, fill, fees.taker, "stop", index)
                elif position.take_profit is not None and high > float(position.take_profit):
                    close_position(symbol, position.take_profit, fees.maker, "take_profit", index)

            for symbol, order in list(pending.items()):
                low = price(symbol, "low", index)
                if low < float(order.limit) and symbol not in positions:
                    cost = order.quantity * order.limit
                    fee = cost * fees.maker
                    cash -= cost + fee
                    fees_paid += fee
                    positions[symbol] = Position(
                        symbol=symbol,
                        module=order.intent.module,
                        quantity=order.quantity,
                        entry_price=order.limit,
                        stop_price=order.stop,
                        take_profit=order.take_profit,
                        initial_risk=order.limit - order.stop,
                        opened_index=index,
                        entry_fee=fee,
                        highest_close=price(symbol, "close", index),
                        decision_id=order.decision_id,
                    )
                    del pending[symbol]
                    if low <= float(order.stop):  # conservative: the stop came after the fill in the same bar
                        slip = self.costs.slippage_for(symbol)
                        fill = min(order.stop, order.limit) * (1 - slip)
                        close_position(symbol, fill, fees.taker, "stop_same_bar", index)
                elif index >= order.expires_index:
                    del pending[symbol]
                    log(index, symbol, "CANCEL", "entry limit not filled before timeout", order.intent)

            # 2. Mark to market at liquidation value.
            market_value = ZERO
            for symbol, position in positions.items():
                close = price(symbol, "close", index)
                position.highest_close = max(position.highest_close, close)
                liquidation = to_decimal(close) * (1 - self.costs.slippage_for(symbol)) * (1 - fees.taker)
                market_value += position.quantity * liquidation
            equity = cash + market_value
            equity_curve[step] = float(equity)
            exposure_curve[step] = float(market_value / equity) if equity > 0 else 0.0

            # 3. Loss limits.
            tehran_day = now.tz_convert(TEHRAN).date()
            if tehran_day != day_key:
                day_key, day_start_equity = tehran_day, equity
            if not halted and equity / self.initial_equity - 1 <= self.limits.kill_switch:
                halted = True
                notes.append(f"kill switch at {now.isoformat()}: equity {equity:.0f}")
                log(index, "*", "KILL", f"equity {equity:.0f} breached the kill switch; flatten and halt")
            if blocked_day != tehran_day and equity / day_start_equity - 1 <= self.limits.daily_loss_limit:
                blocked_day = tehran_day
                log(
                    index, "*", "BLOCK", f"daily loss limit hit ({equity / day_start_equity - 1:.4f}); no entries today"
                )

            minutes_left = (end - now).total_seconds() / 60
            flatten = halted or minutes_left <= self.limits.flatten_before_end_minutes
            entries_allowed = (
                not halted and blocked_day != tehran_day and minutes_left > self.limits.no_entry_before_end_minutes
            )
            if flatten:
                pending.clear()
                for symbol in positions:
                    exits[symbol] = "kill_switch" if halted else "end_of_window"
            if index == last:
                break

            # 4. Strategy intents, then risk sizing.
            view = PortfolioView(
                equity=equity,
                cash=cash,
                positions={
                    symbol: PositionView(
                        symbol=symbol,
                        module=p.module,
                        entry_price=p.entry_price,
                        quantity=p.quantity,
                        stop_price=p.stop_price,
                        take_profit=p.take_profit,
                        initial_risk=p.initial_risk,
                        opened_index=p.opened_index,
                        highest_close=p.highest_close,
                    )
                    for symbol, p in positions.items()
                },
                pending_entries=frozenset(pending),
                entries_allowed=entries_allowed,
            )
            if flatten:
                continue
            exposure = market_value + sum((o.quantity * o.limit for o in pending.values()), ZERO)
            for intent in self.strategy.on_cycle(ctx, view):
                symbol = intent.symbol
                if intent.kind == "EXIT" and symbol in positions:
                    exits[symbol] = intent.reason
                    log(index, symbol, "EXIT", intent.reason, intent)
                elif intent.kind == "SET_STOP" and symbol in positions and intent.stop_price is not None:
                    new_stop = to_decimal(intent.stop_price)
                    if new_stop > positions[symbol].stop_price:  # stops only tighten
                        positions[symbol].stop_price = new_stop
                elif intent.kind == "ENTER":
                    if not entries_allowed or symbol in positions or symbol in pending:
                        continue  # one position per symbol, no averaging down
                    rules = self.data.rules[symbol]
                    limit = floor_to_decimals(to_decimal(intent.limit_price), rules.price_decimals)
                    stop = floor_to_decimals(to_decimal(intent.stop_price), rules.price_decimals)
                    decision = size_entry(
                        equity=equity,
                        exposure=exposure,
                        entry_price=limit,
                        stop_price=stop,
                        size_fraction=to_decimal(intent.size_fraction),
                        amount_decimals=rules.amount_decimals,
                        exchange_min_notional=rules.min_notional,
                        limits=self.limits,
                    )
                    if decision.quantity <= 0:
                        log(index, symbol, "REJECT", intent.reason, intent, decision.reason)
                        continue
                    take_profit = (
                        floor_to_decimals(to_decimal(intent.take_profit), rules.price_decimals)
                        if intent.take_profit
                        else None
                    )
                    decision_id = log(index, symbol, "ENTER", intent.reason, intent, decision.reason)
                    pending[symbol] = PendingEntry(
                        intent=intent,
                        quantity=decision.quantity,
                        limit=limit,
                        stop=stop,
                        take_profit=take_profit,
                        expires_index=index + intent.timeout_bars,
                        decision_id=decision_id,
                    )
                    exposure += decision.notional

        # Anything still open (it should not be) is closed at the last close with taker costs.
        for symbol in list(positions):
            notes.append(f"{symbol} still open at the end; closed at the last close")
            fill = to_decimal(price(symbol, "close", last)) * (1 - self.costs.slippage_for(symbol))
            close_position(symbol, fill, fees.taker, "forced_end", last)
        final_equity = cash
        equity_curve[-1] = float(final_equity)
        return BacktestResult(
            start=clock[first],
            end=clock[last],
            initial_equity=self.initial_equity,
            final_equity=final_equity,
            equity=pd.Series(equity_curve, index=clock[first : last + 1]),
            trades=trades,
            decisions=decisions,
            fees_paid=fees_paid,
            exposure=pd.Series(exposure_curve, index=clock[first : last + 1]),
            final_tmn_share=Decimal(1) if not positions else ZERO,
            halted=halted,
            notes=notes,
        )
