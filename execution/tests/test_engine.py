from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from backtest.costs import Fees
from backtest.engine import MarketData
from execution.broker import OrderState, ShadowBroker, apply_resource, parse_balances
from execution.engine import Engine, EngineSettings
from execution.journal import Journal
from risk.guard import RiskGuard, SymbolRules
from risk.limits import RiskLimits
from strategy.base import FeatureFrame, Intent

RULES = {"BTCTMN": SymbolRules(8, 0, Decimal("50000"))}
FEES = Fees(Decimal("0.0025"), Decimal("0.0035"))


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeFeed:
    """Best bid/ask and public trades under test control; one 5m bar per call to `bar()`."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.rules = RULES
        self.bid, self.ask = Decimal("100000"), Decimal("100100")
        self.trades: list[dict] = []
        self.closes: list[float] = [100000.0] * 30
        self.close_time = pd.Timestamp("2026-10-22T10:00:00Z")

    def refresh(self, now) -> int:
        return 0

    def last_bar_close(self):
        return self.close_time

    def bar(self, close: float) -> None:
        self.closes.append(close)
        self.close_time += pd.Timedelta(minutes=5)

    def market_data(self, extra=None):
        clock = pd.date_range(end=self.close_time, periods=len(self.closes), freq="5min")
        frame = pd.DataFrame(
            {"close": self.closes, "open": self.closes, "high": self.closes, "low": self.closes, "volume": 1.0},
            index=clock,
        )
        return MarketData(clock, {"BTCTMN": FeatureFrame(frame)}, RULES, ["BTCTMN"]), {}

    def best_bid_ask(self, symbol):
        return self.bid, self.ask

    def trades_since(self, symbol, iso):
        since = datetime.fromisoformat(iso)
        return [t for t in self.trades if t["time"] > since]


class Script:
    name = "script"

    def __init__(self) -> None:
        self.queue: list[Intent] = []

    def on_cycle(self, ctx, portfolio):
        out, self.queue = self.queue, []
        return out


def _entry(limit=100000.0, stop=98000.0, tp=106000.0, size=1.0) -> Intent:
    return Intent(
        kind="ENTER",
        symbol="BTCTMN",
        module="trend",
        reason="test entry",
        limit_price=limit,
        stop_price=stop,
        take_profit=tp,
        size_fraction=size,
    )


def _engine(tmp_path, end_in_hours=24.0, **limit_overrides):
    clock = Clock(datetime(2026, 10, 22, 10, 0, 30, tzinfo=UTC))
    feed = FakeFeed(clock)
    broker = ShadowBroker(feed, FEES, Decimal("5000000"))
    limits = RiskLimits(
        max_position_fraction=Decimal("0.15"),
        max_total_exposure=Decimal("0.45"),
        kill_switch=Decimal("-0.025"),
        daily_loss_limit=Decimal("-0.02"),
        min_order_tmn=Decimal("50000"),
        **limit_overrides,
    )
    settings = EngineSettings(
        mode="shadow",
        challenge_end=clock.now + timedelta(hours=end_in_hours),
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
        Journal(tmp_path / "j", "shadow", "test"),
        tmp_path / "state",
        clock=clock,
    )
    return engine, feed, broker, strategy, clock


def _decisions(tmp_path) -> list[dict]:
    import json

    return [
        json.loads(line)
        for f in sorted((tmp_path / "j").glob("decisions-*.jsonl"))
        for line in f.read_text().splitlines()
    ]


def _cycle(engine, feed, clock, close=100000.0) -> None:
    feed.bar(close)
    clock.advance(seconds=15)
    engine.step()


def test_entry_fill_places_take_profit_then_stop_cancels_it_and_sells(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    engine.step()  # start, reconcile
    strategy.queue = [_entry()]
    _cycle(engine, feed, clock)
    entry = next(t for t in engine.orders.values() if t.role == "entry")
    assert entry.order.side == "BUY" and entry.order.type == "LIMIT" and entry.order.price == Decimal("100000")
    # ramp: first 2 h at half size: 15% of 5M = 750k -> 375k
    assert entry.order.quantity * entry.order.price <= Decimal("375000")
    feed.trades.append({"price": Decimal("99990"), "quantity": Decimal("1"), "time": clock.now + timedelta(seconds=1)})
    clock.advance(seconds=15)
    engine.step()
    position = engine.positions["BTCTMN"]
    assert position.quantity > 0 and position.tp_client_id
    tp = engine.orders[position.tp_client_id].order
    assert tp.side == "SELL" and tp.price == Decimal("106000")
    feed.bid, feed.ask = Decimal("97900"), Decimal("98000")  # below the stop
    clock.advance(seconds=15)
    engine.step()
    clock.advance(seconds=15)
    engine.step()  # the exit fill is processed on the next loop
    kinds = [d["kind"] for d in _decisions(tmp_path)]
    assert kinds.count("EXIT") == 1  # never two sells for one position
    assert "STOP" in kinds and "CLOSE" in kinds
    assert tp.status == "CANCELED"  # the resting take-profit was cancelled before the market sell
    assert "BTCTMN" not in engine.positions
    assert broker.balances().get("BTC", Decimal(0)) <= Decimal("0.00000001")


def test_unfilled_entry_is_cancelled_after_the_timeout(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    engine.step()
    strategy.queue = [_entry()]
    _cycle(engine, feed, clock)
    clock.advance(minutes=16)
    engine.step()
    entry = next(t for t in engine.orders.values() if t.role == "entry")
    assert entry.order.status == "CANCELED" and not engine.positions


def test_end_of_challenge_flattens_with_a_limit_then_market(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path, end_in_hours=3.0)
    engine.step()
    strategy.queue = [_entry(tp=None)]
    _cycle(engine, feed, clock)
    feed.trades.append({"price": Decimal("99990"), "quantity": Decimal("1"), "time": clock.now + timedelta(seconds=1)})
    clock.advance(seconds=15)
    engine.step()
    assert "BTCTMN" in engine.positions
    clock.advance(hours=2, minutes=1)  # 59 minutes before the end
    engine.step()
    flatten = next(t for t in engine.orders.values() if t.role == "flatten_limit")
    assert flatten.order.side == "SELL" and flatten.order.price == feed.ask
    clock.advance(seconds=125)  # the limit was not filled: market after the timeout
    engine.step()
    clock.advance(seconds=15)
    engine.step()
    assert any(t.role == "exit" and t.order.type == "MARKET" for t in engine.orders.values())
    assert not engine.positions
    sells = [t for t in engine.orders.values() if t.order.side == "SELL" and t.order.status == "FILLED"]
    assert len(sells) == 1  # one sell in total
    strategy.queue = [_entry()]
    _cycle(engine, feed, clock)  # no new entries in the last 2 h
    assert not any(t.role == "entry" and not t.order.done for t in engine.orders.values())
    clock.advance(hours=1)
    engine.step()
    final = [d for d in _decisions(tmp_path) if d["kind"] == "FINAL"]
    assert final and final[0]["ok"] is True


def test_kill_switch_flattens_and_halts(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    engine.step()
    broker.ledger["TMN"] = Decimal("4800000")  # -4%: beyond the -2.5% kill switch
    clock.advance(seconds=15)
    engine.step()
    assert engine.halted
    strategy.queue = [_entry()]
    _cycle(engine, feed, clock)
    assert not any(t.role == "entry" for t in engine.orders.values())
    assert "KILL" in [d["kind"] for d in _decisions(tmp_path)]


def test_ramp_ends_after_two_hours(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    engine.step()
    assert engine.ramp(clock.now) == Decimal("0.5")
    assert engine.ramp(clock.now + timedelta(hours=2, minutes=1)) == Decimal(1)


def test_unknown_open_order_halts_new_entries(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    engine.step()
    broker.orders["foreign"] = OrderState("foreign", "BTCTMN", "BUY", "LIMIT", Decimal("1"), Decimal("1"))
    clock.advance(seconds=31)
    engine.step()
    assert engine.entries_halted


def test_restart_restores_positions_and_orders(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    engine.step()
    strategy.queue = [_entry()]
    _cycle(engine, feed, clock)
    feed.trades.append({"price": Decimal("99990"), "quantity": Decimal("1"), "time": clock.now + timedelta(seconds=1)})
    clock.advance(seconds=15)
    engine.step()
    ledger = dict(broker.ledger)
    fresh = ShadowBroker(feed, FEES, Decimal("5000000"))  # a restarted process starts with a new broker
    again = Engine(engine.s, feed, fresh, engine.guard, strategy, engine.journal, tmp_path / "state", clock=clock)
    again.step()
    assert fresh.ledger == ledger  # the simulated balances survived the restart
    assert "BTCTMN" in again.positions and again.positions["BTCTMN"].tp_client_id
    assert any(t.role == "take_profit" for t in again.orders.values())


def test_order_state_comes_from_active_and_quantities_not_the_status_text():
    order = OrderState("c", "BTCTMN", "BUY", "LIMIT", Decimal("1"), Decimal("2"))
    assert apply_resource(order, {"active": True, "executedQty": "0", "origQty": "2", "status": "?"}).status == "NEW"
    assert apply_resource(order, {"active": True, "executedQty": "1", "origQty": "2"}).status == "PARTIAL"
    assert apply_resource(order, {"active": False, "executedQty": "2", "origQty": "2"}).status == "FILLED"
    assert apply_resource(order, {"active": False, "executedQty": "1", "origQty": "2"}).status == "CANCELED"
    assert (
        apply_resource(order, {"active": False, "executedQty": "0", "origQty": "2", "status": "REJECTED"}).status
        == "REJECTED"
    )
    filled = apply_resource(
        order,
        {
            "active": False,
            "executedQty": "2",
            "origQty": "2",
            "fee": "0.004",
            "fills": [{"feeAsset": "BTC", "price": "1", "quantity": "2"}],
        },
    )
    assert filled.fee == Decimal("0.004") and filled.fee_asset == "BTC"


def test_balance_parser_accepts_value_or_available_plus_locked():
    assert parse_balances({"result": {"balances": {"TMN": {"asset": "TMN", "value": "5000000"}}}}) == {
        "TMN": Decimal("5000000")
    }
    assert parse_balances({"result": {"BTC": {"available": "0.1", "locked": "0.2"}}}) == {"BTC": Decimal("0.3")}
    with pytest.raises(Exception, match="unrecognized"):
        parse_balances({"result": {}})


def _ai_entry(symbol="BTCTMN", group="ai_activity:slot") -> Intent:
    return Intent(
        kind="ENTER",
        symbol=symbol,
        module="ai_activity",
        reason="test ai entry",
        limit_price=99000.0,
        stop_price=99000.0 * 0.99,
        take_profit=99000.0 * 1.008,
        size_fraction=1.0,
        group=group,
        limit_at_bid=True,
        entry_timeout_minutes=3.0,
        reprices=1,
    )


def test_ai_entry_is_priced_at_the_best_bid_one_per_group_and_repriced_once(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    engine.step()
    second = _ai_entry()
    strategy.queue = [_ai_entry(), second]  # same group: only the first approved one becomes an order
    _cycle(engine, feed, clock)
    entries = [t for t in engine.orders.values() if t.role == "entry"]
    assert len(entries) == 1 and entries[0].order.price == Decimal("100000")  # the best bid, not 99000
    assert Decimal(entries[0].meta["take_profit"]) == Decimal("100800")
    feed.bid, feed.ask = Decimal("100500"), Decimal("100600")
    clock.advance(minutes=3, seconds=5)
    engine.step()  # timeout: cancel, then one reprice at the new bid
    entries = [t for t in engine.orders.values() if t.role == "entry"]
    assert len(entries) == 2 and entries[1].order.price == Decimal("100500") and not entries[1].order.done
    assert Decimal(entries[1].meta["stop"]) == Decimal("100500") * Decimal("0.99")
    clock.advance(minutes=3, seconds=5)
    engine.step()  # second timeout: cancel, no more reprices
    entries = [t for t in engine.orders.values() if t.role == "entry"]
    assert len(entries) == 2 and all(t.order.status == "CANCELED" for t in entries)
    assert [d["kind"] for d in _decisions(tmp_path)].count("REPRICE") == 1


def test_error_halt_lifts_after_clean_loops_but_a_reconcile_halt_does_not(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    engine.step()
    healthy = feed.refresh

    def down(now):
        raise OSError("nodename nor servname provided")

    feed.refresh = down
    for _ in range(5):
        clock.advance(seconds=61)
        engine.step()
    assert engine.entries_halted
    feed.refresh = healthy
    for _ in range(engine.s.resume_after_clean_loops):
        clock.advance(seconds=15)
        engine.step()
    assert not engine.entries_halted
    assert "RESUME_ENTRIES" in [d["kind"] for d in _decisions(tmp_path)]
    broker.orders["foreign"] = OrderState("foreign", "BTCTMN", "BUY", "LIMIT", Decimal("1"), Decimal("1"))
    for _ in range(engine.s.resume_after_clean_loops + 5):
        clock.advance(seconds=31)
        engine.step()
    assert engine.entries_halted  # unknown orders on the account: stays halted


def test_a_slow_llm_review_does_not_eat_the_entry_timeout_and_the_price_is_the_fresh_bid(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)

    class SlowAgent:
        def risk_mode(self, ctx, view):
            return "NORMAL"

        def review(self, intent, ctx, view):
            clock.advance(seconds=90)  # a local model under load
            feed.bid, feed.ask = Decimal("99800"), Decimal("99900")
            return None

    engine.agent = SlowAgent()
    engine.step()
    strategy.queue = [_ai_entry()]
    _cycle(engine, feed, clock)
    (entry,) = [t for t in engine.orders.values() if t.role == "entry"]
    assert entry.order.price == Decimal("99800")
    assert Decimal(entry.meta["stop"]) == Decimal("98802")  # 99800 * 0.99, rounded up to the tick
    clock.advance(minutes=2)
    engine.step()
    assert not entry.order.done  # 3 minutes from the order, not from the cycle start


def test_no_entry_on_a_market_without_recent_trades(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    feed.stale_symbols = lambda: {"BTCTMN": 240.0}
    engine.step()
    strategy.queue = [_ai_entry()]
    _cycle(engine, feed, clock)
    assert not any(t.role == "entry" for t in engine.orders.values())
    skip = [d for d in _decisions(tmp_path) if d["kind"] == "SKIP"]
    assert skip and "240 min" in skip[0]["risk"]


def test_rounding_dust_is_marked_and_swept_into_the_exit(tmp_path):
    engine, feed, broker, strategy, clock = _engine(tmp_path)
    feed.rules = {"BTCTMN": SymbolRules(5, 0, Decimal("50000"))}  # a coarse amount step, like ETHTMN
    engine.step()
    strategy.queue = [_entry(tp=None)]
    _cycle(engine, feed, clock)
    feed.trades.append({"price": Decimal("99990"), "quantity": Decimal("1"), "time": clock.now + timedelta(seconds=1)})
    clock.advance(seconds=15)
    engine.step()
    position = engine.positions["BTCTMN"]
    held = broker.balances()["BTC"]
    assert held > position.quantity  # the coin-side fee left dust below one amount step
    assert engine.mark() > broker.balances()["TMN"] + position.quantity * feed.bid * Decimal("0.9965")
    broker.ledger["BTC"] += Decimal("0.00001")  # dust left by an earlier trade
    feed.bid, feed.ask = Decimal("97900"), Decimal("98000")  # stop
    clock.advance(seconds=15)
    engine.step()
    clock.advance(seconds=15)
    engine.step()
    assert broker.balances()["BTC"] < Decimal("0.00001")  # everything sellable was sold, dust included
