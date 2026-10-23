from agent.roles import candidate_context
from strategy.base import Intent


def test_the_trade_agent_sees_named_smc_fields_without_price_levels():
    intent = Intent(
        kind="ENTER",
        symbol="BTCTMN",
        module="smc_long",
        reason="SMC setup",
        signals={"smc_pattern": "sweep then FVG", "smc_rr": 1.8, "smc_sweep_depth": 0.009, "smc_discount": 0.31},
        limit_price=100.0,
        stop_price=99.0,
        take_profit=102.0,
    )
    context = candidate_context(intent)
    assert context["module"] == "smc_long" and context["smc_pattern"] == "sweep then FVG"
    assert context["smc_rr"] == 1.8 and context["smc_discount"] == 0.31
    assert "limit_price" not in context and not any(isinstance(v, float) and v > 50 for v in context.values())
