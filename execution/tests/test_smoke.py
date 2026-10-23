from decimal import Decimal

from execution import smoke
from risk.guard import SymbolRules


class FakeBroker:
    def __init__(self, fill=False):
        self.fill, self.open = fill, {}

    def place(self, order):
        self.open[order.client_id] = order
        if self.fill:
            order.executed_qty = order.quantity
        return order

    def refresh(self, order):
        return order

    def cancel(self, order):
        self.open.pop(order.client_id, None)
        order.status = "CANCELED" if order.executed_qty < order.quantity else "FILLED"
        return order

    def open_orders(self):
        return [{"clientOrderId": cid} for cid in self.open]


def test_plan_is_far_below_the_bid_and_just_above_the_minimum():
    p = smoke.plan("USDTTMN", Decimal("271620"), SymbolRules(2, 0, Decimal("50000")))
    assert p.price == Decimal("258039") and p.notional >= Decimal("55000") and p.quantity == Decimal("0.22")


def test_run_confirms_the_cancel_and_flags_a_fill():
    p = smoke.plan("USDTTMN", Decimal("271620"), SymbolRules(2, 0, Decimal("50000")))
    ok = smoke.run(FakeBroker(), p, lambda: {"value": "5000000", "locked": "0"})
    assert ok.ok and [s["step"] for s in ok.steps][-2:] == ["open orders after cancel", "balance after"]
    bad = smoke.run(FakeBroker(fill=True), p, lambda: {})
    assert not bad.ok and any("FILLED" in problem for problem in bad.problems)


def _shadow(buy_fee_in):
    from execution.broker import ShadowBroker
    from execution.tests.test_engine import FEES, Clock, FakeFeed

    clock = Clock(__import__("datetime").datetime(2026, 10, 23, 9, tzinfo=__import__("datetime").UTC))
    feed = FakeFeed(clock)
    return ShadowBroker(feed, FEES, Decimal("5000000"), buy_fee_in=buy_fee_in), feed


def test_round_trip_reports_the_fee_currency_and_closes():
    for fee_in, asset in (("coin", "BTC"), ("TMN", "TMN")):
        broker, feed = _shadow(fee_in)
        result = smoke.round_trip(broker, "BTCTMN", SymbolRules(8, 0, Decimal("50000")), feed.best_bid_ask, lambda s: 0)
        assert result.ok, result.problems
        assert result.fee_buy["asset"] == asset and Decimal(result.fee_buy["rate"]) > 0
        assert result.leftover < Decimal("0.00000001") and result.cost_tmn > 0


def test_round_trip_fails_when_the_sell_cannot_close():
    broker, feed = _shadow("coin")
    original = broker.place

    def place(order):
        if order.side == "SELL":
            order.status, order.error = "REJECTED", "simulated"
            return order
        return original(order)

    broker.place = place
    result = smoke.round_trip(broker, "BTCTMN", SymbolRules(8, 0, Decimal("50000")), feed.best_bid_ask, lambda s: 0)
    assert not result.ok and "did not close" in result.problems[0]
