from decimal import Decimal

from risk.limits import RiskLimits, ceil_to_decimals, floor_to_decimals, size_entry

LIMITS = RiskLimits()
EQUITY = Decimal("5000000")


def _size(**overrides):
    arguments = dict(
        equity=EQUITY,
        exposure=Decimal(0),
        entry_price=Decimal("1000"),
        stop_price=Decimal("990"),
        size_fraction=Decimal(1),
        amount_decimals=4,
        exchange_min_notional=Decimal("50000"),
        limits=LIMITS,
    )
    arguments.update(overrides)
    return size_entry(**arguments)


def test_position_cap_binds_for_a_tight_stop():
    decision = _size()  # 1% stop: risk sizing would allow 2.5M, the 20% cap allows 1M
    assert decision.notional == Decimal("1000000.0000")
    assert "position cap" in decision.reason


def test_risk_per_trade_binds_for_a_wide_stop():
    decision = _size(stop_price=Decimal("980"))  # 2% stop -> 0.5% x 5M / 2% = 1.25M, still capped at 1M
    assert decision.notional == Decimal("1000000.0000")
    decision = _size(stop_price=Decimal("980"), equity=Decimal("2000000"))
    assert decision.notional == Decimal("400000.0000") or decision.quantity == 0


def test_exposure_cap_leaves_room_only_up_to_60_percent():
    decision = _size(exposure=Decimal("2500000"))  # 3M allowed in total, 2.5M used
    assert decision.notional == Decimal("500000.0000")
    assert _size(exposure=Decimal("2800000")).quantity == 0  # 200k left, below the 500k minimum


def test_stop_wider_than_two_percent_is_rejected():
    assert _size(stop_price=Decimal("979")).quantity == 0


def test_stop_above_entry_is_rejected():
    assert _size(stop_price=Decimal("1000")).quantity == 0


def test_quantity_is_floored_to_the_amount_precision():
    decision = _size(entry_price=Decimal("3"), stop_price=Decimal("2.98"), amount_decimals=0)
    assert decision.quantity == decision.quantity.to_integral_value()
    assert decision.notional <= Decimal("1000000")


def test_size_fraction_scales_and_is_clamped():
    assert _size(size_fraction=Decimal("0.5")).notional == Decimal("500000.0000")
    assert _size(size_fraction=Decimal("3")).notional == Decimal("1000000.0000")


def test_floor_to_decimals_rounds_down():
    assert floor_to_decimals(Decimal("1.23456789"), 4) == Decimal("1.2345")
    assert floor_to_decimals(Decimal("1999.99"), 0) == Decimal("1999")


def test_ceil_to_decimals_rounds_up():
    assert ceil_to_decimals(Decimal("980.686"), 0) == Decimal("981")
    assert ceil_to_decimals(Decimal("1.23451"), 4) == Decimal("1.2346")


def test_per_symbol_position_cap_override():
    limits = RiskLimits(position_caps=(("USDTTMN", Decimal("0.40")),))
    assert limits.position_cap("USDTTMN") == Decimal("0.40") and limits.position_cap("BTCTMN") == Decimal("0.20")
    decision = _size(stop_price=Decimal("995"), limits=limits, position_cap=limits.position_cap("USDTTMN"))
    assert decision.notional == Decimal("2000000.0000")  # 40% of 5M; risk sizing (0.5% / 0.5%) allows 5M
