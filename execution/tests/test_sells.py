"""Every live SELL uses the available balance at that moment, floored to the amount step (never the bought
quantity); dust below the minimum order is logged and not sold; a rejected sell is retried once."""

from datetime import timedelta
from decimal import Decimal

from execution.broker import ShadowBroker, parse_available
from execution.engine import Engine, EngineSettings
from execution.journal import Journal
from execution.tests.test_engine import FEES, Clock, FakeFeed, Script, _cycle, _decisions, _entry
from risk.guard import RiskGuard, SymbolRules
from risk.limits import RiskLimits


class FakeExchange(ShadowBroker):
    """ShadowBroker with exchange behaviours: buy fee in the coin or in TMN, a partial fill, one stale read."""

    def __init__(self, feed, buy_fee_in="coin", partial: Decimal | None = None) -> None:
        super().__init__(feed, FEES, Decimal("5000000"), buy_fee_in=buy_fee_in)
        self.partial = partial
        self.stuck: set[str] = set()
        self.stale_reads = 0  # the next N available() reads report this much extra coin
        self.stale_extra = Decimal(0)

    def _fill(self, order, price, fee_rate):
        if order.client_id in self.stuck:
            return None  # the rest of a partially filled order never trades
        if self.partial is None or order.side != "BUY":
            return super()._fill(order, price, fee_rate)
        self.stuck.add(order.client_id)
        part = (order.quantity * self.partial).quantize(Decimal("0.00000001"))
        base = order.symbol.removesuffix("TMN")
        fee = part * fee_rate
        self.ledger["TMN"] -= part * price
        self.ledger[base] = self.ledger.get(base, Decimal(0)) + part - fee
        order.executed_qty, order.fee, order.fee_asset, order.avg_price = part, fee, base, price
        order.status = "PARTIAL"
        self.partial = None
        return None

    def available(self):
        out = super().available()
        if self.stale_reads:
            self.stale_reads -= 1
            out["BTC"] = out.get("BTC", Decimal(0)) + self.stale_extra
        return out


def _live(tmp_path, amount_decimals=8, **exchange):
    clock = Clock(__import__("datetime").datetime(2026, 10, 22, 10, 0, 30, tzinfo=__import__("datetime").UTC))
    feed = FakeFeed(clock)
    feed.rules = {"BTCTMN": SymbolRules(amount_decimals, 0, Decimal("50000"))}
    broker = FakeExchange(feed, **exchange)
    limits = RiskLimits(
        max_position_fraction=Decimal("0.15"),
        max_total_exposure=Decimal("0.45"),
        kill_switch=Decimal("-0.025"),
        daily_loss_limit=Decimal("-0.02"),
        min_order_tmn=Decimal("50000"),
    )
    settings = EngineSettings(
        mode="live",
        challenge_end=clock.now + timedelta(hours=24),
        ramp_hours=2.0,
        ramp_factor=Decimal("0.5"),
        start_equity=Decimal("5000000"),
    )
    strategy = Script()
    engine = Engine(
        settings,
        feed,
        broker,
        RiskGuard(limits),
        strategy,
        Journal(tmp_path / "j", "live", "t"),
        tmp_path / "s",
        clock=clock,
    )
    engine.step()
    return engine, feed, broker, strategy, clock


def _buy(engine, feed, strategy, clock, tp=106000.0):
    strategy.queue = [_entry(tp=tp)]
    _cycle(engine, feed, clock)
    feed.trades.append({"price": Decimal("99990"), "quantity": Decimal("9"), "time": clock.now + timedelta(seconds=1)})
    clock.advance(seconds=15)
    engine.step()


def _sells(engine):
    return [t for t in engine.orders.values() if t.order.side == "SELL" and t.order.status != "REJECTED"]


def _stop_out(engine, feed, clock):
    feed.bid, feed.ask = Decimal("97900"), Decimal("98000")
    clock.advance(seconds=15)
    engine.step()
    clock.advance(seconds=15)
    engine.step()


def test_fee_in_coin_the_take_profit_and_the_exit_sell_the_available_balance(tmp_path):
    engine, feed, broker, strategy, clock = _live(tmp_path)
    _buy(engine, feed, strategy, clock)
    entry = next(t.order for t in engine.orders.values() if t.role == "entry")
    (tp,) = _sells(engine)
    assert tp.order.quantity < entry.quantity  # bought quantity minus the coin-side fee
    assert tp.order.quantity == broker.ledger["BTC"].quantize(Decimal("0.00000001"), rounding="ROUND_DOWN")
    _stop_out(engine, feed, clock)
    assert broker.ledger["BTC"] < Decimal("0.00000001") and "BTCTMN" not in engine.positions


def test_fee_in_tmn_the_full_bought_quantity_is_sold(tmp_path):
    engine, feed, broker, strategy, clock = _live(tmp_path, buy_fee_in="TMN")
    _buy(engine, feed, strategy, clock)
    entry = next(t.order for t in engine.orders.values() if t.role == "entry")
    (tp,) = _sells(engine)
    assert tp.order.quantity == entry.quantity == broker.ledger["BTC"]
    _stop_out(engine, feed, clock)
    assert broker.ledger["BTC"] == 0


def test_partial_fill_sells_only_what_was_received(tmp_path):
    engine, feed, broker, strategy, clock = _live(tmp_path, partial=Decimal("0.4"))
    _buy(engine, feed, strategy, clock)
    clock.advance(minutes=16)  # the entry times out with 40% filled
    engine.step()
    entry = next(t.order for t in engine.orders.values() if t.role == "entry")
    assert entry.status == "CANCELED" and entry.executed_qty > 0
    (tp,) = _sells(engine)
    assert tp.order.quantity <= entry.executed_qty and tp.order.quantity <= broker.ledger["BTC"]
    _stop_out(engine, feed, clock)
    assert broker.ledger["BTC"] < Decimal("0.00000001")


def test_dust_below_the_minimum_order_is_logged_not_sold_and_left_out_of_the_final_check(tmp_path):
    engine, feed, broker, strategy, clock = _live(tmp_path)
    _buy(engine, feed, strategy, clock)
    for tracked in list(engine.orders.values()):  # leave only dust: 0.0001 BTC = 10 TMN
        if tracked.role == "take_profit":
            broker.cancel(tracked.order)
    broker.ledger["BTC"] = Decimal("0.0001")
    _stop_out(engine, feed, clock)
    kinds = [d["kind"] for d in _decisions(tmp_path)]
    assert "DUST" in kinds and "BTCTMN" not in engine.positions
    assert not any(t.role == "exit" for t in engine.orders.values())
    engine.final_check()
    final = [d for d in _decisions(tmp_path) if d["kind"] == "FINAL"][-1]
    assert final["ok"] is True and Decimal(final["dust_tmn"]) > 0


def test_an_insufficient_balance_rejection_is_retried_once_with_a_fresh_floored_balance(tmp_path):
    engine, feed, broker, strategy, clock = _live(tmp_path)
    broker.stale_reads, broker.stale_extra = 1, Decimal("0.0001")  # the first read after the fill is too high
    _buy(engine, feed, strategy, clock)
    kinds = [d["kind"] for d in _decisions(tmp_path)]
    assert kinds.count("SELL_REJECTED") == 1 and kinds.count("SELL_RETRY") == 1
    (tp,) = _sells(engine)
    assert tp.order.quantity <= broker.ledger["BTC"] and engine.positions["BTCTMN"].tp_client_id == tp.order.client_id


def test_available_is_value_minus_locked_unless_the_exchange_sends_it():
    data = {"result": {"balances": {"BTC": {"asset": "BTC", "value": "0.5", "locked": "0.2"}}}}
    assert parse_available(data) == {"BTC": Decimal("0.3")}
    assert parse_available({"result": {"BTC": {"available": "0.4", "locked": "0.1"}}}) == {"BTC": Decimal("0.4")}
