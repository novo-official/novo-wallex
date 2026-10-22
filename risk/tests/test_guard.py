from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.conf import settings

from exchange.symbols import load_market_policy
from risk.guard import AccountState, LLMAdjustment, RiskGuard, SymbolRules
from risk.limits import RiskLimits
from strategy.base import Intent

RULES = SymbolRules(amount_decimals=8, price_decimals=0, min_notional=Decimal("50000"))
NOW = datetime(2026, 10, 25, 8, 0, tzinfo=UTC)


def _state(**overrides) -> AccountState:
    values = dict(
        now=NOW,
        equity=Decimal("5000000"),
        initial_equity=Decimal("5000000"),
        day_start_equity=Decimal("5000000"),
        exposure=Decimal(0),
        minutes_to_end=3000.0,
    )
    values.update(overrides)
    return AccountState(**values)


def _intent(symbol="BTCTMN", limit=22_000_000_000.0, stop_fraction=0.02, tp_fraction=0.06, size=1.0) -> Intent:
    return Intent(
        kind="ENTER",
        symbol=symbol,
        module="trend",
        reason="test",
        limit_price=limit,
        stop_price=limit * (1 - stop_fraction),
        take_profit=limit * (1 + tp_fraction),
        size_fraction=size,
    )


@pytest.fixture
def guard():
    policy = load_market_policy(settings.TRADING_CONFIG_FILE, settings.BLOCKED_MARKETS_FILE)
    return RiskGuard(RiskLimits(), policy)


def test_approves_a_normal_entry_within_the_position_cap(guard):
    decision = guard.check_entry(_intent(), _state(), RULES)
    assert decision.approved
    assert decision.notional <= Decimal("1000000") and decision.notional > Decimal("999000")
    assert (decision.limit - decision.stop) / decision.limit <= Decimal("0.02")


@pytest.mark.parametrize("symbol", ["OPTMN", "BTCUSDT", "XLMTMN"])
def test_market_policy_rejects_blocked_and_non_allowlisted(guard, symbol):
    decision = guard.check_entry(_intent(symbol=symbol), _state(), RULES)
    assert not decision.approved and "market policy" in decision.reason


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"equity": Decimal("4690000")}, "kill switch"),
        ({"equity": Decimal("4840000")}, "daily loss limit"),
        ({"minutes_to_end": 119.0}, "last 120 minutes"),
        ({"risk_mode": "OFF"}, "risk_mode OFF"),
        ({"halted": True}, "kill switch"),
        ({"daily_blocked": True}, "daily loss limit"),
    ],
)
def test_account_status_blocks_entries(guard, overrides, expected):
    decision = guard.check_entry(_intent(), _state(**overrides), RULES)
    assert not decision.approved and expected in decision.reason


def test_status_flags_flatten_in_the_last_hour_and_on_kill(guard):
    assert guard.status(_state(minutes_to_end=59.0)).flatten_required
    assert not guard.status(_state(minutes_to_end=61.0)).flatten_required
    status = guard.status(_state(equity=Decimal("4690000")))
    assert status.trigger_kill and status.flatten_required


def test_one_position_per_symbol_and_self_trade_guard(guard):
    assert not guard.check_entry(_intent(), _state(open_symbols=frozenset({"BTCTMN"})), RULES).approved
    assert not guard.check_entry(_intent(), _state(pending_symbols=frozenset({"BTCTMN"})), RULES).approved
    decision = guard.check_entry(_intent(), _state(resting_sell_symbols=frozenset({"BTCTMN"})), RULES)
    assert not decision.approved and decision.actions == ("cancel_resting_sell:BTCTMN",)


BIG = dict(equity=Decimal("10000000"), initial_equity=Decimal("10000000"), day_start_equity=Decimal("10000000"))


def test_llm_can_only_reduce_risk(guard):
    # 10M equity: half of the 2M position cap stays above the 500k minimum order.
    base = guard.check_entry(_intent(), _state(**BIG), RULES)
    bigger = guard.check_entry(_intent(size=0.5), _state(**BIG), RULES, LLMAdjustment(size_fraction=1.0))
    assert float(bigger.notional) == pytest.approx(
        float(base.notional) / 2, rel=1e-6
    )  # the LLM cannot raise 0.5 to 1.0
    smaller = guard.check_entry(_intent(), _state(**BIG), RULES, LLMAdjustment(size_fraction=0.6))
    assert float(smaller.notional) == pytest.approx(float(base.notional) * 0.6, rel=1e-6)

    intent = _intent()
    looser = guard.check_entry(intent, _state(), RULES, LLMAdjustment(stop_price=intent.stop_price * 0.99))
    assert looser.stop == base.stop  # a wider stop is ignored
    tighter = guard.check_entry(intent, _state(), RULES, LLMAdjustment(stop_price=intent.limit_price * 0.99))
    assert tighter.stop > base.stop

    farther = guard.check_entry(intent, _state(), RULES, LLMAdjustment(take_profit=intent.take_profit * 1.1))
    assert farther.take_profit == base.take_profit
    too_close = guard.check_entry(intent, _state(), RULES, LLMAdjustment(take_profit=intent.limit_price * 1.001))
    assert too_close.take_profit == base.take_profit  # below the fee floor: ignored
    closer = guard.check_entry(intent, _state(), RULES, LLMAdjustment(take_profit=intent.limit_price * 1.03))
    assert closer.take_profit < base.take_profit

    rejected = guard.check_entry(intent, _state(), RULES, LLMAdjustment(approve=False))
    assert not rejected.approved and "LLM" in rejected.reason


def test_defensive_mode_halves_the_size(guard):
    normal = guard.check_entry(_intent(), _state(**BIG), RULES)
    defensive = guard.check_entry(_intent(), _state(risk_mode="DEFENSIVE", **BIG), RULES)
    assert float(defensive.notional) == pytest.approx(float(normal.notional) / 2, rel=1e-6)


def test_liquidity_buffer(guard):
    ok = guard.check_entry(_intent(), _state(liquidity_limit=Decimal("4000000")), RULES)
    assert ok.approved
    # worst case 5M - 1M x 2% = 4.98M; a 4.2M limit with a 20% buffer needs 5.04M
    near = guard.check_entry(_intent(), _state(liquidity_limit=Decimal("4200000")), RULES)
    assert not near.approved and "liquidity buffer" in near.reason


def test_unknown_risk_mode_fails_closed(guard):
    assert not guard.check_entry(_intent(), _state(risk_mode="AGGRESSIVE"), RULES).approved


def test_half_size_below_the_minimum_order_is_rejected(guard):
    decision = guard.check_entry(_intent(), _state(risk_mode="DEFENSIVE"), RULES)  # 499,950 < 500,000
    assert not decision.approved and "below the minimum" in decision.reason
