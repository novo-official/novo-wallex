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
