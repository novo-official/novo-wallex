"""G3 smoke test: one tiny LIMIT BUY far below the best bid, read back, cancel, confirm the cancel.

Never runs without a typed confirmation. It records what the account endpoints really return (order fields,
whether balances' `value` includes `locked`, the fee rate) so the live engine's UNVERIFIED parsers can be checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from .broker import OrderState, new_client_id


@dataclass
class SmokePlan:
    symbol: str
    best_bid: Decimal
    price: Decimal
    quantity: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


def plan(symbol: str, best_bid: Decimal, rules, below: Decimal = Decimal("0.05")) -> SmokePlan:
    """Price `below` under the bid; the smallest quantity worth 1.1 x the exchange minimum order."""
    price = (best_bid * (1 - below)).quantize(Decimal(1).scaleb(-rules.price_decimals), rounding=ROUND_DOWN)
    quantity = (rules.min_notional * Decimal("1.1") / price).quantize(
        Decimal(1).scaleb(-rules.amount_decimals), rounding=ROUND_CEILING
    )
    return SmokePlan(symbol, best_bid, price, quantity)


@dataclass
class SmokeResult:
    ok: bool = False
    steps: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _order_fields(order: OrderState) -> dict:
    return {
        "client_id": order.client_id,
        "status": order.status,
        "raw_status": order.raw_status,
        "executed_qty": str(order.executed_qty),
        "fee": str(order.fee),
        "fee_asset": order.fee_asset,
        "error": order.error,
    }


def run(broker, smoke_plan: SmokePlan, balances_raw) -> SmokeResult:
    """`broker`: LiveBroker (or a fake). `balances_raw()`: the raw TMN balance entry, for the value/locked check."""
    result = SmokeResult()
    result.steps.append({"step": "balance before", "TMN": balances_raw()})
    order = OrderState(new_client_id("S"), smoke_plan.symbol, "BUY", "LIMIT", smoke_plan.price, smoke_plan.quantity)
    broker.place(order)
    result.steps.append({"step": "place", **_order_fields(order)})
    if order.status == "REJECTED":
        result.problems.append(f"order rejected: {order.error}")
        return result
    broker.refresh(order)
    result.steps.append({"step": "read back", **_order_fields(order)})
    result.steps.append({"step": "balance with the order resting", "TMN": balances_raw()})
    if order.executed_qty > 0:
        result.problems.append("the order (partly) FILLED: a small position exists, sell it in the engine or by hand")
    broker.cancel(order)
    broker.refresh(order)
    result.steps.append({"step": "after cancel", **_order_fields(order)})
    open_ids = {str(o.get("clientOrderId") or o.get("client_id") or "") for o in broker.open_orders()}
    result.steps.append({"step": "open orders after cancel", "count": len(open_ids - {""})})
    if order.status != "CANCELED" or order.client_id in open_ids:
        result.problems.append(
            f"cancel not confirmed: status {order.status}, still open: {order.client_id in open_ids}"
        )
    result.steps.append({"step": "balance after", "TMN": balances_raw()})
    result.ok = not result.problems
    return result


@dataclass
class RoundTripResult:
    ok: bool = False
    steps: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    fee_buy: dict = field(default_factory=dict)
    fee_sell: dict = field(default_factory=dict)
    leftover: Decimal = Decimal(0)
    leftover_tmn: Decimal = Decimal(0)
    cost_tmn: Decimal = Decimal(0)


def _wait_done(broker, order: OrderState, wait, timeout_s: float) -> OrderState:
    waited = 0.0
    while order.status not in ("FILLED", "CANCELED", "REJECTED") and waited < timeout_s:
        wait(2.0)
        waited += 2.0
        broker.refresh(order)
    return order


def round_trip(broker, symbol: str, rules, best_bid_ask, wait, timeout_s: float = 60.0) -> RoundTripResult:
    """One minimal real round trip: LIMIT BUY at the best ask (just above the minimum order), then a MARKET SELL
    of the whole available balance. Measures which currency each fee is charged in, the fee rates and the dust.
    Not ok (abort go-live) if the buy fills but the sell cannot close the position."""
    result = RoundTripResult()
    base = symbol.removesuffix("TMN")
    step = Decimal(1).scaleb(-rules.amount_decimals)
    before = broker.available()
    _, ask = best_bid_ask(symbol)
    quantity = (rules.min_notional * Decimal("1.1") / ask).quantize(step, rounding=ROUND_CEILING)
    buy = OrderState(new_client_id("R"), symbol, "BUY", "LIMIT", ask, quantity)
    broker.place(buy)
    _wait_done(broker, buy, wait, timeout_s)
    result.steps.append({"step": "buy at the best ask", **_order_fields(buy), "price": str(ask)})
    if buy.status not in ("FILLED", "CANCELED", "REJECTED"):
        broker.cancel(buy)
        broker.refresh(buy)
    if buy.executed_qty <= 0:
        result.problems.append(f"the buy did not fill ({buy.status} {buy.error}); nothing bought")
        return result
    after_buy = broker.available()
    received = after_buy.get(base, Decimal(0)) - before.get(base, Decimal(0))
    spent = before.get("TMN", Decimal(0)) - after_buy.get("TMN", Decimal(0))
    notional = buy.executed_qty * (buy.avg_price or ask)
    if buy.fee > 0 and buy.fee_asset:  # the order's own fee field (the balance is rounded to the amount step)
        quote = buy.executed_qty if buy.fee_asset.upper() == base else notional
        result.fee_buy = {"asset": buy.fee_asset.upper(), "rate": str(buy.fee / quote) if quote else "?"}
    elif received < buy.executed_qty:
        result.fee_buy = {"asset": base, "rate": str((buy.executed_qty - received) / buy.executed_qty)}
    else:
        result.fee_buy = {"asset": "TMN", "rate": str((spent - notional) / notional) if notional else "?"}
    result.fee_buy |= {"order_fee": str(buy.fee), "order_fee_asset": buy.fee_asset}
    result.steps.append({"step": "after the buy", "received": str(received), "tmn_spent": str(spent)})
    sell_qty = after_buy.get(base, Decimal(0)).quantize(step, rounding=ROUND_DOWN)
    bid, _ = best_bid_ask(symbol)
    if sell_qty * bid < rules.min_notional:
        result.problems.append(f"cannot sell {sell_qty} {base}: below the minimum order")
        return result
    # MARKET orders are refused on the prop account: an aggressive LIMIT under the bid, re-placed up to 3 times.
    sold_qty, sold_value, sell = Decimal(0), Decimal(0), None
    for attempt in range(1, 4):
        sell_qty = broker.available().get(base, Decimal(0)).quantize(step, rounding=ROUND_DOWN)
        bid, _ = best_bid_ask(symbol)
        if sell_qty * bid < rules.min_notional:
            break
        price = (bid * (1 - Decimal("0.002"))).quantize(Decimal(1).scaleb(-rules.price_decimals), rounding=ROUND_DOWN)
        sell = OrderState(new_client_id("R"), symbol, "SELL", "LIMIT", price, sell_qty)
        broker.place(sell)
        _wait_done(broker, sell, wait, 20.0)
        if sell.status not in ("FILLED", "CANCELED", "REJECTED"):
            broker.cancel(sell)
            broker.refresh(sell)
        result.steps.append(
            {"step": f"aggressive limit sell {attempt}", **_order_fields(sell), "price": str(price), "bid": str(bid)}
        )
        sold_qty += sell.executed_qty
        sold_value += sell.executed_qty * (sell.avg_price or price)
        if sell.status in ("FILLED", "REJECTED"):
            break
    after_sell = broker.available()
    got = after_sell.get("TMN", Decimal(0)) - after_buy.get("TMN", Decimal(0))
    result.fee_sell = {
        "asset": (sell.fee_asset if sell else "") or "TMN",
        "rate": str((sold_value - got) / sold_value) if sold_value else "?",
        "order_fee": str(sell.fee) if sell else "0",
    }
    result.leftover = after_sell.get(base, Decimal(0)) - before.get(base, Decimal(0))
    result.leftover_tmn = result.leftover * bid
    result.cost_tmn = before.get("TMN", Decimal(0)) - after_sell.get("TMN", Decimal(0))
    result.steps.append(
        {"step": "after the sell", "leftover": str(result.leftover), "leftover_tmn": str(result.leftover_tmn)}
    )
    if result.leftover >= step:
        status = sell.status if sell else "not sent"
        result.problems.append(f"the sell did not close the position: status {status}, {result.leftover} {base} left")
    result.ok = not result.problems
    return result
