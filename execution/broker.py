"""Brokers: the only place orders are sent. LiveBroker (Wallex) and ShadowBroker (simulated) share one interface,
so the engine's code path is identical in shadow and live.

Safety:
  * the market policy is checked immediately before every order;
  * a POST is never retried blindly: after a transport error the order is looked up by its client_id;
  * order state comes from `active` + executedQty vs origQty (the Swagger leaves the status enum undocumented);
  * every request/response is journaled by the engine (journal.py), keys never are.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

import httpx

from exchange.client import WallexAPIError, WallexClient, WallexError, WallexTransportError
from exchange.symbols import MarketPolicy

ZERO = Decimal(0)


def new_client_id(prefix: str) -> str:
    """Unique, idempotency key for an order: nv<prefix><UTC time><random>, at most 32 characters."""
    return f"nv{prefix[:3]}{datetime.now(UTC):%y%m%d%H%M%S}{secrets.token_hex(4)}"


@dataclass
class OrderState:
    client_id: str
    symbol: str
    side: str  # BUY / SELL
    type: str  # LIMIT / MARKET
    price: Decimal | None
    quantity: Decimal
    status: str = "NEW"  # NEW / PARTIAL / FILLED / CANCELED / REJECTED
    executed_qty: Decimal = ZERO
    avg_price: Decimal | None = None
    fee: Decimal = ZERO
    fee_asset: str = ""
    raw_status: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    error: str = ""

    @property
    def done(self) -> bool:
        return self.status in ("FILLED", "CANCELED", "REJECTED")


def _dec(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def apply_resource(order: OrderState, resource: dict) -> OrderState:
    """Update `order` from a Swagger OrderResource. Status from active/executedQty/origQty, raw status kept."""
    executed = _dec(resource.get("executedQty"))
    original = _dec(resource.get("origQty")) or order.quantity
    active = resource.get("active")
    raw = str(resource.get("status") or "")
    order.raw_status = raw
    order.executed_qty = executed
    price = _dec(resource.get("executedPrice"))
    order.avg_price = price if price > 0 else order.avg_price
    order.fee = _dec(resource.get("fee"))
    fills = resource.get("fills") or []
    if fills:
        order.fee_asset = str(fills[0].get("feeAsset") or "")
        if order.avg_price is None:
            quantity = sum(_dec(f.get("quantity")) for f in fills)
            if quantity > 0:
                order.avg_price = sum(_dec(f.get("price")) * _dec(f.get("quantity")) for f in fills) / quantity
    if original > 0 and executed >= original:
        order.status = "FILLED"
    elif active is False or active == 0:
        lowered = raw.lower()
        order.status = "REJECTED" if executed == 0 and ("reject" in lowered or "expire" in lowered) else "CANCELED"
    elif executed > 0:
        order.status = "PARTIAL"
    else:
        order.status = "NEW"
    return order


class Broker(Protocol):
    name: str

    def place(self, order: OrderState) -> OrderState: ...
    def cancel(self, order: OrderState) -> OrderState: ...
    def refresh(self, order: OrderState) -> OrderState: ...
    def open_orders(self) -> list[dict]: ...
    def balances(self) -> dict[str, Decimal]: ...


class LiveBroker:
    """Wallex prop account. Constructed only by the engine in live mode after preflight."""

    name = "live"

    def __init__(self, client: WallexClient, policy: MarketPolicy, journal) -> None:
        if not client._http.headers.get("x-api-key"):
            raise WallexError("live broker needs WALLEX_API_KEY")
        self.client = client
        self.policy = policy
        self.journal = journal

    def _send(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        response = self.client._http.request(method, path, json=body)  # one attempt: no blind POST retry
        self.journal.write(
            "http",
            {
                "method": method,
                "path": path,
                "body": body,
                "status": response.status_code,
                "response": _safe_json(response),
            },
        )
        return response

    def place(self, order: OrderState) -> OrderState:
        self.policy.check(order.symbol)  # immediately before every order
        body = {
            "symbol": order.symbol,
            "side": order.side,
            "type": order.type,
            "quantity": format(order.quantity, "f"),
            "client_id": order.client_id,
        }
        if order.type == "LIMIT":
            body["price"] = format(order.price, "f")
        try:
            response = self._send("POST", "/v1/account/orders", body)
        except httpx.TransportError as exc:
            # The order may or may not exist: look it up before anything else.
            order.error = f"transport error on POST: {exc}"
            return self.refresh(order)
        if response.status_code >= 400:
            order.status, order.error = "REJECTED", f"HTTP {response.status_code}: {response.text[:300]}"
            return order
        result = (_safe_json(response) or {}).get("result") or {}
        return apply_resource(order, result) if result else order

    def cancel(self, order: OrderState) -> OrderState:
        try:
            response = self._send("DELETE", f"/v1/account/orders/{order.client_id}")
        except httpx.TransportError as exc:
            order.error = f"transport error on DELETE: {exc}"
            return self.refresh(order)
        if response.status_code >= 400:
            order.error = f"cancel HTTP {response.status_code}: {response.text[:200]}"
            return self.refresh(order)
        result = (_safe_json(response) or {}).get("result") or {}
        return apply_resource(order, result) if result else self.refresh(order)

    def refresh(self, order: OrderState) -> OrderState:
        try:
            data = self.client.get_json(f"/v1/account/orders/{order.client_id}")
        except (WallexAPIError, WallexTransportError) as exc:
            order.error = f"refresh failed: {exc}"
            return order
        result = (data or {}).get("result") or {}
        return apply_resource(order, result) if result else order

    def open_orders(self) -> list[dict]:
        data = self.client.get_json("/v1/account/openOrders")
        result = (data or {}).get("result") or {}
        if isinstance(result, dict):
            return [order for orders in result.values() for order in (orders or [])]
        return list(result)

    def balances(self) -> dict[str, Decimal]:
        """Total (available + locked) per asset. Shape UNVERIFIED until the step-0 probe with the key."""
        data = self.client.get_json("/v1/account/balances")
        return parse_balances(data)


def parse_balances(data: dict) -> dict[str, Decimal]:
    result = (data or {}).get("result") or {}
    balances = result.get("balances", result) if isinstance(result, dict) else {}
    out = {}
    for asset, entry in (balances or {}).items():
        if isinstance(entry, dict):
            value = (
                _dec(entry.get("value"))
                if "value" in entry
                else _dec(entry.get("available")) + _dec(entry.get("locked"))
            )
            out[str(entry.get("asset") or asset).upper()] = value
    if not out:
        raise WallexError(f"unrecognized balances shape: {json.dumps(data)[:300]}")
    return out


def _safe_json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return {"text": response.text[:300]}


class ShadowBroker:
    """Simulated fills against the live public book and trades (GET only), with the configured fees.

    A resting limit BUY fills when a later public trade prints below its price (maker fee) or when the best
    ask drops to it (taker fee at the ask); SELL is symmetric. MARKET fills at the best bid/ask (taker)."""

    name = "shadow"

    def __init__(self, feed, fees, start_tmn: Decimal) -> None:
        self.feed = feed  # market feed: best_bid_ask(symbol), trades_since(symbol, iso)
        self.fees = fees
        self.ledger: dict[str, Decimal] = {"TMN": start_tmn}
        self.orders: dict[str, OrderState] = {}

    def _fill(self, order: OrderState, price: Decimal, fee_rate: Decimal) -> None:
        base = order.symbol.removesuffix("TMN")
        quantity = order.quantity - order.executed_qty
        notional = quantity * price
        if order.side == "BUY":
            fee = quantity * fee_rate  # fee in the received asset, as on most exchanges
            self.ledger["TMN"] = self.ledger.get("TMN", ZERO) - notional
            self.ledger[base] = self.ledger.get(base, ZERO) + quantity - fee
            order.fee_asset = base
        else:
            fee = notional * fee_rate
            self.ledger[base] = self.ledger.get(base, ZERO) - quantity
            self.ledger["TMN"] = self.ledger.get("TMN", ZERO) + notional - fee
            order.fee_asset = "TMN"
        order.fee += fee
        order.avg_price = price
        order.executed_qty = order.quantity
        order.status = "FILLED"

    def place(self, order: OrderState) -> OrderState:
        self.orders[order.client_id] = order
        bid, ask = self.feed.best_bid_ask(order.symbol)
        if order.type == "MARKET":
            self._fill(order, ask if order.side == "BUY" else bid, self.fees.taker)
        elif order.side == "BUY" and ask is not None and ask <= order.price:
            self._fill(order, ask, self.fees.taker)
        elif order.side == "SELL" and bid is not None and bid >= order.price:
            self._fill(order, bid, self.fees.taker)
        return order

    def refresh(self, order: OrderState) -> OrderState:
        if order.done or order.type != "LIMIT":
            return order
        for trade in self.feed.trades_since(order.symbol, order.created_at):
            price = Decimal(str(trade["price"]))
            if (order.side == "BUY" and price < order.price) or (order.side == "SELL" and price > order.price):
                self._fill(order, order.price, self.fees.maker)
                break
        return order

    def cancel(self, order: OrderState) -> OrderState:
        self.refresh(order)
        if not order.done:
            order.status = "CANCELED"
        return order

    def open_orders(self) -> list[dict]:
        return [{"clientOrderId": o.client_id, "symbol": o.symbol} for o in self.orders.values() if not o.done]

    def balances(self) -> dict[str, Decimal]:
        return dict(self.ledger)
