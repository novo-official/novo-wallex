"""Fill rules, end-of-window flatten and loss limits of the backtester, on hand-made bars."""

from decimal import Decimal

import pandas as pd
import pytest

from backtest.costs import CostModel, Fees
from backtest.engine import Backtester, MarketData
from risk.guard import SymbolRules
from risk.limits import RiskLimits
from strategy.base import FeatureFrame, Intent

START = pd.Timestamp("2026-10-01T00:00:00Z")
FEES = Fees(maker=Decimal("0.0025"), taker=Decimal("0.0035"))
NO_SLIP = CostModel(FEES, {"AAATMN": Decimal(0)}, default_slippage=Decimal(0))


def _market(bars: list[tuple[float, float, float, float]], volume: float = 1.0) -> MarketData:
    clock = pd.date_range(START, periods=len(bars), freq="5min", tz="UTC")
    frame = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=clock)
    frame["volume"] = volume
    return MarketData(
        clock=clock,
        features={"AAATMN": FeatureFrame(frame)},
        rules={"AAATMN": SymbolRules(amount_decimals=4, price_decimals=0, min_notional=Decimal("50000"))},
        symbols=["AAATMN"],
    )


class Scripted:
    """Emits the given intents at the given clock indexes."""

    name = "scripted"

    def __init__(self, script: dict[int, list[Intent]]) -> None:
        self.script = script
        self.seen_entries_allowed: dict[int, bool] = {}

    def on_cycle(self, ctx, portfolio):
        self.seen_entries_allowed[ctx.index] = portfolio.entries_allowed
        return self.script.get(ctx.index, [])


def _enter(limit=1000.0, stop=990.0, tp=1030.0, timeout=3) -> Intent:
    return Intent(
        kind="ENTER",
        symbol="AAATMN",
        module="test",
        reason="test",
        limit_price=limit,
        stop_price=stop,
        take_profit=tp,
        timeout_bars=timeout,
    )


def _run(bars, script, limits=None, costs=NO_SLIP, hours_after=0.0):
    data = _market(bars)
    strategy = Scripted(script)
    end = data.clock[-1] + pd.Timedelta(hours=hours_after)
    result = Backtester(
        data, strategy, costs, limits or RiskLimits(no_entry_before_end_minutes=0, flatten_before_end_minutes=0)
    ).run(data.clock[0], end)
    return result, strategy


FLAT = (1005.0, 1006.0, 1004.0, 1005.0)


def test_limit_touching_but_not_trading_through_does_not_fill():
    bars = [FLAT, (1005, 1006, 1000, 1003), FLAT, FLAT, FLAT, FLAT]  # low == limit, never below
    result, _ = _run(bars, {0: [_enter()]})
    assert result.trades == [] and result.final_equity == Decimal("5000000")
    assert any(d["action"] == "CANCEL" for d in result.decisions)


def test_limit_fills_at_limit_with_maker_fee_and_take_profit_needs_trade_through():
    bars = [FLAT, (1005, 1006, 999, 1001), (1001, 1030, 1000, 1020), (1020, 1031, 1019, 1025), FLAT]
    result, _ = _run(bars, {0: [_enter()]})
    (trade,) = result.trades
    assert trade.entry_price == Decimal("1000") and trade.exit_price == Decimal("1030")
    assert trade.exit_reason == "take_profit"
    cost = trade.quantity * Decimal("1000")
    proceeds = trade.quantity * Decimal("1030")
    assert trade.fees == cost * FEES.maker + proceeds * FEES.maker
    assert trade.pnl == proceeds - cost - trade.fees
    assert result.final_equity == Decimal("5000000") + trade.pnl


def test_stop_wins_when_stop_and_take_profit_are_in_the_same_bar():
    bars = [FLAT, (1005, 1006, 999, 1001), (1001, 1040, 980, 1000), FLAT]
    result, _ = _run(bars, {0: [_enter()]})
    (trade,) = result.trades
    assert trade.exit_reason == "stop" and trade.exit_price == Decimal("990")


def test_entry_bar_reaching_the_stop_is_stopped_in_that_bar():
    bars = [FLAT, (1005, 1006, 985, 1001), FLAT]
    result, _ = _run(bars, {0: [_enter()]})
    (trade,) = result.trades
    assert trade.exit_reason == "stop_same_bar" and trade.pnl < 0


def test_gap_below_stop_fills_at_the_open_with_taker_fee_and_slippage():
    costs = CostModel(FEES, {"AAATMN": Decimal("0.001")})
    bars = [FLAT, (1005, 1006, 999, 1001), (970, 975, 960, 965), FLAT]
    result, _ = _run(bars, {0: [_enter()]}, costs=costs)
    (trade,) = result.trades
    assert trade.exit_price == Decimal("970") * (1 - Decimal("0.001"))


def test_window_ends_flat_and_blocks_late_entries():
    limits = RiskLimits()  # no entries in the last 2 h, flatten in the last 60 min
    bars = [FLAT, (1005, 1006, 999, 1001)] + [FLAT] * 40
    result, strategy = _run(bars, {0: [_enter(tp=2000)]}, limits=limits)
    (trade,) = result.trades
    assert trade.exit_reason == "end_of_window"
    assert result.final_tmn_share == 1
    last_index = len(bars) - 1
    assert strategy.seen_entries_allowed[0] is True
    assert strategy.seen_entries_allowed[last_index - 24] is False  # 2 h before the end


def test_kill_switch_flattens_and_halts():
    limits = RiskLimits(kill_switch=Decimal("-0.001"), no_entry_before_end_minutes=0, flatten_before_end_minutes=0)
    bars = [FLAT, (1005, 1006, 999, 1001), (995, 996, 991, 992), FLAT, FLAT, FLAT]
    result, strategy = _run(bars, {0: [_enter(stop=985)]}, limits=limits)
    assert result.halted
    (trade,) = result.trades
    assert trade.exit_reason == "kill_switch"
    assert strategy.seen_entries_allowed.get(4) in (False, None)


def test_daily_loss_limit_blocks_entries_for_the_rest_of_the_day():
    limits = RiskLimits(
        daily_loss_limit=Decimal("-0.0001"), no_entry_before_end_minutes=0, flatten_before_end_minutes=0
    )
    bars = [FLAT, (1005, 1006, 999, 1001), (995, 996, 985, 986), FLAT, FLAT, FLAT]
    result, strategy = _run(bars, {0: [_enter()], 4: [_enter(limit=1004, stop=995)]}, limits=limits)
    assert len(result.trades) == 1
    assert strategy.seen_entries_allowed[4] is False
    assert any(d["action"] == "BLOCK" for d in result.decisions)


def test_one_position_per_symbol_no_averaging_down():
    bars = [FLAT, (1005, 1006, 999, 1001), (1001, 1002, 995, 996), FLAT, FLAT]
    result, _ = _run(bars, {0: [_enter(stop=985, tp=5000)], 1: [_enter(limit=998, stop=985, tp=5000)]})
    assert len(result.trades) == 1  # the second ENTER was ignored


def test_stops_only_tighten():
    loosen = Intent(kind="SET_STOP", symbol="AAATMN", module="test", reason="loosen", stop_price=950.0)
    bars = [FLAT, (1005, 1006, 999, 1001), FLAT, (1001, 1002, 985, 990), FLAT]
    result, _ = _run(bars, {0: [_enter()], 2: [loosen]})
    (trade,) = result.trades
    assert trade.exit_reason == "stop" and trade.exit_price == Decimal("990")


@pytest.mark.parametrize("bad_window", [(0, 0)])
def test_empty_window_raises(bad_window):
    data = _market([FLAT, FLAT])
    with pytest.raises(ValueError):
        Backtester(data, Scripted({}), NO_SLIP, RiskLimits()).run(START, START)


def test_stop_at_exactly_the_cap_survives_price_rounding():
    # limit 1000.7 floors to 1000 (0 decimals); a 2% stop at 980.686 must round up to 981, not down to 980.
    bars = [FLAT, (1005, 1006, 999, 1001), FLAT, FLAT]
    result, _ = _run(bars, {0: [_enter(limit=1000.7, stop=1000.7 * 0.98, tp=2000)]})
    assert [d["action"] for d in result.decisions][0] == "ENTER"
    assert len(result.trades) == 1


def test_taker_entry_fills_at_the_next_open_with_taker_fee():
    taker = Intent(
        kind="ENTER",
        symbol="AAATMN",
        module="test",
        reason="t",
        limit_price=1000.0,
        stop_price=985.0,
        take_profit=5000.0,
        entry_type="taker",
    )
    bars = [FLAT, (1007, 1008, 1006, 1007), FLAT, FLAT]  # never trades below 1000: a maker limit would not fill
    result, _ = _run(bars, {0: [taker]})
    (trade,) = result.trades
    assert trade.entry_price == Decimal("1007")
    assert trade.fees > trade.quantity * Decimal("1007") * FEES.taker  # taker in + taker out at the end


def test_maker_exit_fills_only_when_traded_through_else_falls_back_to_taker():
    def exit_intent():
        return Intent(kind="EXIT", symbol="AAATMN", module="test", reason="done", exit_type="maker", timeout_bars=2)

    bars = [FLAT, (1005, 1006, 999, 1001), (1001, 1002, 1000, 1001), (1001, 1003, 1000, 1002), FLAT, FLAT]
    result, _ = _run(bars, {0: [_enter(tp=5000)], 2: [exit_intent()]})
    (trade,) = result.trades
    assert trade.exit_reason == "done (maker)" and trade.exit_price == Decimal("1001")

    flat_after = [FLAT, (1005, 1006, 999, 1001), (1001, 1001, 1000, 1001)] + [(1001, 1001, 1000, 1000)] * 4
    result, _ = _run(flat_after, {0: [_enter(tp=5000)], 2: [exit_intent()]})
    (trade,) = result.trades
    assert trade.exit_reason == "done (maker timeout, taker)"
