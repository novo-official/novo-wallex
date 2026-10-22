"""The trading engine: one loop, identical in shadow and live (only the broker differs).

Every `loop_seconds`:
  1. refresh orders (by client_id) and act on fills: entry -> position (+ resting take-profit), take-profit /
     exit / flatten fills -> position closed; maker entries past their timeout are cancelled;
  2. mark equity at liquidation value (best bid, minus the taker fee) and check stops against the best bid;
  3. RiskGuard status: kill switch (flatten + halt), daily block, end-of-challenge flatten in the last 60 min;
  4. on every new 5m close: the strategy's intents (same code as the backtest), the LLM layer, RiskGuard,
     the ramp (x0.5 for the first 2 h), then maker-limit entry orders;
  5. every `reconcile_seconds`: open orders and balances against local state;
  6. heartbeat + state file; consecutive errors -> new entries halted (exits keep running).
Stops are watched by the engine (best bid every loop) rather than resting on the exchange, so no second
sell order competes with the resting take-profit; the supervisor restarts the engine within seconds.
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pandas as pd

from risk.guard import AccountState, RiskGuard
from strategy.base import Context, PortfolioView, PositionView

from .broker import OrderState, new_client_id

logger = logging.getLogger(__name__)
ZERO = Decimal(0)


def floor_decimals(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


@dataclass
class LivePosition:
    symbol: str
    module: str
    quantity: Decimal
    entry_price: Decimal
    stop: Decimal
    take_profit: Decimal | None
    opened_at: str
    opened_index: int
    entry_client_id: str
    decision: dict = field(default_factory=dict)
    tp_client_id: str | None = None
    exit_client_id: str | None = None
    highest_close: float = 0.0


@dataclass
class TrackedOrder:
    order: OrderState
    role: str  # entry / take_profit / exit / flatten_limit / flatten_market
    expires_at: str | None = None
    meta: dict = field(default_factory=dict)
    processed: bool = False  # its final state (fill, cancel) has been acted on: exactly once


@dataclass
class EngineSettings:
    mode: str
    challenge_end: datetime | None
    loop_seconds: int = 15
    reconcile_seconds: int = 30
    candle_refresh_seconds: int = 60
    entry_timeout_minutes: int = 15
    flatten_limit_timeout_seconds: int = 120
    max_consecutive_errors: int = 5
    ramp_hours: float = 2.0
    ramp_factor: Decimal = Decimal("0.5")
    taker_fee: Decimal = Decimal("0.0035")
    start_equity: Decimal = Decimal("5000000")


class Engine:
    def __init__(
        self,
        settings: EngineSettings,
        feed,
        broker,
        guard: RiskGuard,
        strategy,
        journal,
        state_dir: Path,
        agent=None,
        extra_columns=None,
        clock=None,
    ) -> None:
        self.s = settings
        self.feed = feed
        self.broker = broker
        self.guard = guard
        self.strategy = strategy
        self.journal = journal
        self.state_dir = state_dir
        self.agent = agent
        self.extra_columns = extra_columns  # callable(features) -> {symbol: {column: series}} (live ML)
        self.clock = clock
        self.positions: dict[str, LivePosition] = {}
        self.orders: dict[str, TrackedOrder] = {}
        self.balances: dict[str, Decimal] = {}
        self.started_at: datetime | None = None
        self.halted = False
        self.entries_halted = False
        self.daily_blocked_day = None
        self.day_key = None
        self.day_start_equity: Decimal | None = None
        self.errors = 0
        self.last_cycle_bar: pd.Timestamp | None = None
        self.last_refresh: datetime | None = None
        self.last_reconcile: datetime | None = None
        self.final_checked = False
        self.equity = settings.start_equity

    # --- helpers -----------------------------------------------------------------------------------

    def now(self) -> datetime:
        return self.clock() if self.clock else datetime.now().astimezone()

    def _rules(self, symbol: str):
        return self.feed.rules[symbol]

    def _minutes_to_end(self, now: datetime) -> float | None:
        if self.s.challenge_end is None:
            return None
        return (self.s.challenge_end - now).total_seconds() / 60

    def _track(self, order: OrderState, role: str, expires: datetime | None = None, **meta) -> TrackedOrder:
        order.created_at = self.now().isoformat()  # the engine's clock: shadow fills only count later trades
        placed = self.broker.place(order)
        tracked = TrackedOrder(placed, role, expires.isoformat() if expires else None, meta)
        self.orders[placed.client_id] = tracked
        self.journal.decision(
            "ORDER",
            role=role,
            symbol=order.symbol,
            side=order.side,
            type=order.type,
            price=order.price,
            quantity=order.quantity,
            client_id=order.client_id,
            status=placed.status,
            error=placed.error,
            **meta,
        )
        return tracked

    def _base(self, symbol: str) -> str:
        return symbol.removesuffix("TMN")

    # --- exits -----------------------------------------------------------------------------------

    def _cancel_symbol_orders(self, symbol: str, roles: tuple[str, ...]) -> None:
        for tracked in list(self.orders.values()):
            if tracked.order.symbol == symbol and tracked.role in roles and not tracked.order.done:
                self.broker.cancel(tracked.order)
                self.journal.decision(
                    "CANCEL",
                    symbol=symbol,
                    role=tracked.role,
                    client_id=tracked.order.client_id,
                    status=tracked.order.status,
                )

    def _sell_quantity(self, position: LivePosition) -> Decimal:
        rules = self._rules(position.symbol)
        held = (
            self.balances.get(self._base(position.symbol), position.quantity)
            if self.s.mode == "live"
            else position.quantity
        )
        return floor_decimals(min(position.quantity, held), rules.amount_decimals)

    def exit_position(self, position: LivePosition, reason: str, market: bool = True) -> None:
        """Cancel the resting take-profit (self-trade guard), then sell: market, or limit at the best ask."""
        if position.exit_client_id:
            pending = self.orders[position.exit_client_id].order
            if pending.status not in ("CANCELED", "REJECTED"):
                return  # an exit is working or filled but not yet processed: never sell twice
        self._cancel_symbol_orders(position.symbol, ("take_profit", "entry"))
        quantity = self._sell_quantity(position)
        bid, ask = self.feed.best_bid_ask(position.symbol)
        if quantity <= 0 or (bid and quantity * bid < self._rules(position.symbol).min_notional):
            self.journal.decision("DUST", symbol=position.symbol, quantity=quantity, reason=reason)
            self.positions.pop(position.symbol, None)
            return
        if market:
            order = OrderState(new_client_id("X"), position.symbol, "SELL", "MARKET", None, quantity)
            tracked = self._track(order, "exit", reason=reason)
        else:
            order = OrderState(new_client_id("F"), position.symbol, "SELL", "LIMIT", ask, quantity)
            expires = self.now() + timedelta(seconds=self.s.flatten_limit_timeout_seconds)
            tracked = self._track(order, "flatten_limit", expires, reason=reason)
        position.exit_client_id = tracked.order.client_id
        self.journal.decision(
            "EXIT",
            symbol=position.symbol,
            module=position.module,
            reason=reason,
            quantity=quantity,
            client_id=tracked.order.client_id,
            bid=bid,
            ask=ask,
        )

    def flatten(self, reason: str) -> None:
        for tracked in list(self.orders.values()):
            if tracked.role == "entry" and not tracked.order.done:
                self.broker.cancel(tracked.order)
        for position in list(self.positions.values()):
            self.exit_position(position, reason, market=False)

    # --- order lifecycle ------------------------------------------------------------------------------

    def _open_position(self, tracked: TrackedOrder) -> None:
        order, meta = tracked.order, tracked.meta
        quantity = order.executed_qty
        base = self._base(order.symbol)
        if order.fee_asset.upper() == base and order.fee > 0:
            quantity -= order.fee  # the fee came out of the bought coins
        quantity = floor_decimals(quantity, self._rules(order.symbol).amount_decimals)
        if quantity <= 0:
            return
        price = order.avg_price or order.price
        position = LivePosition(
            order.symbol,
            meta["module"],
            quantity,
            price,
            Decimal(meta["stop"]),
            Decimal(meta["take_profit"]) if meta.get("take_profit") else None,
            self.now().isoformat(),
            int(meta.get("bar_index", 0)),
            order.client_id,
            decision=meta,
        )
        self.positions[order.symbol] = position
        self.journal.decision(
            "FILL",
            symbol=order.symbol,
            module=position.module,
            side="BUY",
            quantity=quantity,
            price=price,
            fee=order.fee,
            fee_asset=order.fee_asset,
            client_id=order.client_id,
        )
        if (
            position.take_profit is not None
            and quantity * position.take_profit >= self._rules(order.symbol).min_notional
        ):
            tp = OrderState(new_client_id("T"), order.symbol, "SELL", "LIMIT", position.take_profit, quantity)
            position.tp_client_id = self._track(tp, "take_profit", symbol_role="tp").order.client_id

    def refresh_orders(self, now: datetime) -> None:
        """Act on every order's state exactly once, including orders that filled the moment they were placed."""
        for client_id, tracked in list(self.orders.items()):
            if tracked.processed:
                continue
            order = tracked.order
            if not order.done:
                self.broker.refresh(order)
            expired = bool(tracked.expires_at) and now >= datetime.fromisoformat(tracked.expires_at)
            if tracked.role == "entry":
                if order.status == "FILLED":
                    tracked.processed = True
                    self._open_position(tracked)
                elif order.done:
                    tracked.processed = True
                    if order.executed_qty > 0:
                        self._open_position(tracked)
                elif expired:
                    self.broker.cancel(order)
                    tracked.processed = True
                    self.journal.decision(
                        "CANCEL",
                        symbol=order.symbol,
                        role="entry",
                        reason="entry timeout",
                        client_id=client_id,
                        executed=order.executed_qty,
                    )
                    if order.executed_qty > 0:
                        self._open_position(tracked)
                continue
            position = self.positions.get(order.symbol)
            if order.status == "FILLED":
                tracked.processed = True
                if position is not None:
                    price = order.avg_price or order.price or ZERO
                    fee_tmn = order.fee if order.fee_asset.upper() in ("TMN", "") else order.fee * price
                    proceeds = order.executed_qty * price - fee_tmn
                    self.journal.decision(
                        "CLOSE",
                        symbol=order.symbol,
                        module=position.module,
                        role=tracked.role,
                        quantity=order.executed_qty,
                        price=price,
                        fee=order.fee,
                        fee_asset=order.fee_asset,
                        pnl_tmn_approx=proceeds - position.quantity * position.entry_price,
                        client_id=client_id,
                    )
                    self.positions.pop(order.symbol, None)
            elif order.done:
                tracked.processed = True  # cancelled (by us) or rejected: the position stays, a new exit may follow
                if position is not None and order.executed_qty > 0:
                    position.quantity -= order.executed_qty  # part of it was sold before the cancel
                if position is not None and position.exit_client_id == client_id:
                    position.exit_client_id = None
            elif tracked.role == "flatten_limit" and expired:
                self.broker.cancel(order)
                tracked.processed = True
                if position is not None:
                    position.quantity -= order.executed_qty
                    position.exit_client_id = None
                    if position.quantity > 0:
                        self.exit_position(position, "flatten: limit timed out, market", market=True)

    # --- marking, risk ------------------------------------------------------------------------------

    def mark(self) -> Decimal:
        value = self.balances.get("TMN", ZERO) if self.balances else self.s.start_equity
        if self.s.mode == "shadow":
            value = self.broker.balances().get("TMN", ZERO)
        for position in self.positions.values():
            bid, _ = self.feed.best_bid_ask(position.symbol)
            if bid is not None:
                value += position.quantity * bid * (1 - self.s.taker_fee)
        return value

    def account_state(self, now: datetime, exposure: Decimal) -> AccountState:
        day = pd.Timestamp(now).tz_convert("Asia/Tehran").date()
        if day != self.day_key:
            self.day_key, self.day_start_equity = day, self.equity
        return AccountState(
            now=now,
            equity=self.equity,
            initial_equity=self.s.start_equity,
            day_start_equity=self.day_start_equity,
            exposure=exposure,
            open_symbols=frozenset(self.positions),
            pending_symbols=frozenset(
                t.order.symbol for t in self.orders.values() if t.role == "entry" and not t.order.done
            ),
            resting_sell_symbols=frozenset(
                t.order.symbol for t in self.orders.values() if t.order.side == "SELL" and not t.order.done
            ),
            minutes_to_end=self._minutes_to_end(now),
            daily_blocked=self.daily_blocked_day == day,
            halted=self.halted,
            risk_mode="NORMAL",
        )

    def exposure(self) -> Decimal:
        open_value = sum((p.quantity * p.entry_price for p in self.positions.values()), ZERO)
        pending = sum(
            (
                t.order.quantity * (t.order.price or 0)
                for t in self.orders.values()
                if t.role == "entry" and not t.order.done
            ),
            ZERO,
        )
        return open_value + pending

    # --- strategy cycle ------------------------------------------------------------------------------

    def ramp(self, now: datetime) -> Decimal:
        if self.started_at and now < self.started_at + timedelta(hours=self.s.ramp_hours):
            return self.s.ramp_factor
        return Decimal(1)

    def cycle(self, now: datetime, state: AccountState, status) -> None:
        data, _ = self.feed.market_data(self.extra_columns)
        ctx = Context(data.clock, data.features, data.symbols)
        ctx.index = len(data.clock) - 1
        bar = data.clock[-1]
        for symbol, position in self.positions.items():
            position.highest_close = max(position.highest_close, ctx.value(symbol, "close"))
        entries = status.entries_allowed and not self.entries_halted
        risk_mode = "NORMAL"
        view = PortfolioView(
            equity=self.equity,
            cash=self.equity,
            entries_allowed=entries,
            pending_entries=state.pending_symbols,
            positions={
                s: PositionView(
                    s,
                    p.module,
                    p.entry_price,
                    p.quantity,
                    p.stop,
                    p.take_profit,
                    p.entry_price - p.stop,
                    p.opened_index,
                    p.highest_close,
                )
                for s, p in self.positions.items()
            },
        )
        if self.agent is not None and entries:
            risk_mode = self.agent.risk_mode(ctx, view)
            if risk_mode == "OFF":
                entries = False
        for intent in self.strategy.on_cycle(ctx, view):
            position = self.positions.get(intent.symbol)
            if intent.kind == "EXIT" and position is not None:
                self.exit_position(position, intent.reason, market=intent.exit_type != "maker")
            elif intent.kind == "SET_STOP" and position is not None and intent.stop_price is not None:
                new_stop = Decimal(repr(intent.stop_price))
                if new_stop > position.stop:
                    position.stop = new_stop
            elif intent.kind == "ENTER" and entries:
                if intent.symbol in self.positions or intent.symbol in state.pending_symbols:
                    continue
                intent = replace(intent, size_fraction=intent.size_fraction * float(self.ramp(now)))
                adjustment = self.agent.review(intent, ctx, view) if self.agent is not None else None
                entry_state = replace(state, exposure=self.exposure(), risk_mode=risk_mode)
                decision = self.guard.check_entry(intent, entry_state, self._rules(intent.symbol), adjustment)
                record = dict(
                    symbol=intent.symbol,
                    module=intent.module,
                    reason=intent.reason,
                    signals=intent.signals,
                    risk=decision.reason,
                    ramp=str(self.ramp(now)),
                    bar=bar.isoformat(),
                    risk_mode=risk_mode,
                )
                if not decision.approved:
                    self.journal.decision("REJECT", **record)
                    continue
                order = OrderState(new_client_id("E"), intent.symbol, "BUY", "LIMIT", decision.limit, decision.quantity)
                expires = now + timedelta(minutes=self.s.entry_timeout_minutes)
                self.journal.decision(
                    "ENTER",
                    **record,
                    client_id=order.client_id,
                    limit=decision.limit,
                    stop=decision.stop,
                    take_profit=decision.take_profit,
                    quantity=decision.quantity,
                )
                self._track(
                    order,
                    "entry",
                    expires,
                    module=intent.module,
                    stop=str(decision.stop),
                    take_profit=str(decision.take_profit) if decision.take_profit else None,
                    bar_index=ctx.index,
                )
        self.last_cycle_bar = bar

    # --- reconcile -----------------------------------------------------------------------------------

    def reconcile(self, now: datetime) -> None:
        self.balances = self.broker.balances()
        ours = {cid for cid, t in self.orders.items() if not t.order.done}
        exchange = {str(o.get("clientOrderId") or o.get("client_id") or "") for o in self.broker.open_orders()}
        unknown = exchange - ours - {""}
        if unknown:
            self.entries_halted = True
            self.journal.decision("RECONCILE", problem="unknown open orders on the account", client_ids=sorted(unknown))
        for position in self.positions.values():
            held = self.balances.get(self._base(position.symbol), ZERO)
            if self.s.mode == "live" and held + Decimal("1e-8") < position.quantity:
                self.journal.decision(
                    "RECONCILE",
                    problem="balance below position",
                    symbol=position.symbol,
                    position=position.quantity,
                    balance=held,
                )
                position.quantity = floor_decimals(held, self._rules(position.symbol).amount_decimals)
        self.last_reconcile = now

    # --- loop ------------------------------------------------------------------------------------------

    def step(self) -> None:
        now = self.now()
        if self.started_at is None:
            self.started_at = now
            self.restore()
            self.reconcile(now)
            if self.s.mode == "live" and not self.positions:
                # Kill switch and daily limit are measured from the real equity at start (preflight: ~100% TMN).
                self.s.start_equity = self.equity = self.mark()
            self.journal.decision(
                "START", equity=self.equity, positions=sorted(self.positions), challenge_end=self.s.challenge_end
            )
        try:
            if self.last_refresh is None or (now - self.last_refresh).total_seconds() >= self.s.candle_refresh_seconds:
                self.feed.refresh(now)
                self.last_refresh = now
            self.refresh_orders(now)
            self.equity = self.mark()
            for position in list(self.positions.values()):
                bid, _ = self.feed.best_bid_ask(position.symbol)
                if bid is not None and bid <= position.stop and not position.exit_client_id:
                    self.journal.decision(
                        "STOP", symbol=position.symbol, module=position.module, bid=bid, stop=position.stop
                    )
                    self.exit_position(position, "stop")
            state = self.account_state(now, self.exposure())
            status = self.guard.status(state)
            if status.trigger_kill:
                self.halted = True
                self.journal.decision("KILL", equity=self.equity, start=self.s.start_equity)
            if status.trigger_daily_block:
                self.daily_blocked_day = pd.Timestamp(now).tz_convert("Asia/Tehran").date()
                self.journal.decision("BLOCK", equity=self.equity, day_start=self.day_start_equity)
            if status.flatten_required:
                self.flatten("kill switch" if self.halted else "end-of-challenge flatten")
            elif self.feed.last_bar_close() != self.last_cycle_bar and self.feed.last_bar_close() is not None:
                close = self.feed.last_bar_close()
                if close.minute % 5 == 0:
                    self.cycle(now, state, status)
            if self.last_reconcile is None or (now - self.last_reconcile).total_seconds() >= self.s.reconcile_seconds:
                self.reconcile(now)
            minutes = self._minutes_to_end(now)
            if minutes is not None and minutes <= 0 and not self.final_checked:
                self.final_check()
            self.errors = 0
        except Exception as exc:  # noqa: BLE001 - the loop must survive; repeated errors halt new entries
            self.errors += 1
            self.journal.decision("ERROR", error=repr(exc), trace=traceback.format_exc()[-1500:], count=self.errors)
            if self.errors >= self.s.max_consecutive_errors and not self.entries_halted:
                self.entries_halted = True
                self.journal.decision("HALT_ENTRIES", reason=f"{self.errors} consecutive errors")
        self.persist(now)

    def restore(self) -> None:
        """Reload positions and open orders after a restart (the exchange stays the truth: reconcile follows)."""
        path = self.state_dir / f"engine_state_{self.s.mode}.json"
        if not path.exists():
            return
        state = json.loads(path.read_text(encoding="utf-8"))
        for symbol, raw in state.get("positions", {}).items():
            take_profit = raw.get("take_profit")
            self.positions[symbol] = LivePosition(
                symbol=symbol,
                module=raw["module"],
                quantity=Decimal(raw["quantity"]),
                entry_price=Decimal(raw["entry_price"]),
                stop=Decimal(raw["stop"]),
                take_profit=Decimal(take_profit) if take_profit not in (None, "None") else None,
                opened_at=raw["opened_at"],
                opened_index=int(raw["opened_index"]),
                entry_client_id=raw["entry_client_id"],
                tp_client_id=None if raw.get("tp_client_id") in (None, "None") else raw["tp_client_id"],
                exit_client_id=None if raw.get("exit_client_id") in (None, "None") else raw["exit_client_id"],
                highest_close=float(raw.get("highest_close") or 0),
            )
        for raw in state.get("open_orders", []):
            order = OrderState(
                raw["client_id"],
                raw["symbol"],
                raw["side"],
                raw["type"],
                Decimal(raw["price"]) if raw.get("price") not in (None, "None") else None,
                Decimal(raw["quantity"]),
                created_at=raw["created_at"],
            )
            self.orders[order.client_id] = TrackedOrder(
                order, raw["role"], raw.get("expires_at"), raw.get("meta") or {}
            )
            self.broker.refresh(order)
        self.halted = bool(state.get("halted"))
        if state.get("start_equity"):
            self.s.start_equity = Decimal(state["start_equity"])
        self.journal.decision("RESTORE", positions=sorted(self.positions), open_orders=len(self.orders))

    def final_check(self) -> None:
        balances = self.broker.balances()
        total = Decimal(balances.get("TMN", ZERO))
        coins = ZERO
        for asset, amount in balances.items():
            if asset != "TMN" and amount > 0 and f"{asset}TMN" in self.feed.rules:
                bid, _ = self.feed.best_bid_ask(f"{asset}TMN")
                coins += amount * (bid or ZERO)
        share = total / (total + coins) if total + coins > 0 else Decimal(1)
        self.journal.decision("FINAL", tmn=total, coins_tmn=coins, tmn_share=share, ok=share >= Decimal("0.99"))
        self.final_checked = True

    def persist(self, now: datetime) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "time": now.isoformat(),
            "mode": self.s.mode,
            "equity": str(self.equity),
            "return": str(self.equity / self.s.start_equity - 1),
            "halted": self.halted,
            "entries_halted": self.entries_halted,
            "errors": self.errors,
            "positions": {
                s: {k: str(v) for k, v in asdict(p).items() if k != "decision"} for s, p in self.positions.items()
            },
            "start_equity": str(self.s.start_equity),
            "open_orders": [
                asdict(t.order) | {"role": t.role, "expires_at": t.expires_at, "meta": t.meta}
                for t in self.orders.values()
                if not t.order.done
            ],
            "last_cycle_bar": self.last_cycle_bar.isoformat() if self.last_cycle_bar is not None else None,
            "challenge_end": self.s.challenge_end.isoformat() if self.s.challenge_end else None,
        }
        temporary = self.state_dir / f"engine_state_{self.s.mode}.json.tmp"
        temporary.write_text(json.dumps(state, default=str, indent=1), encoding="utf-8")
        temporary.replace(self.state_dir / f"engine_state_{self.s.mode}.json")
        (self.state_dir / f"heartbeat_{self.s.mode}.txt").write_text(now.isoformat(), encoding="utf-8")
