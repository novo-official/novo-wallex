import pandas as pd

from strategy.base import Context, FeatureFrame
from strategy.modules.ai_activity import AIActivityModule, AIActivityParams

CLOCK = pd.date_range("2026-10-22T06:00:00Z", periods=40, freq="5min", tz="UTC")
EDGES = {"BTCTMN": 0.001, "ETHTMN": 0.003, "USDTTMN": -0.002}


def _ctx(index: int) -> Context:
    features = {
        symbol: FeatureFrame(pd.DataFrame({"close": 100.0, "volume": 1.0, "ml_net_h24": edge}, index=CLOCK))
        for symbol, edge in EDGES.items()
    }
    ctx = Context(CLOCK, features, list(EDGES))
    ctx.index = index
    return ctx


def test_candidates_are_ranked_by_edge_in_one_group_with_the_trade_plan():
    module = AIActivityModule()
    intents = module.candidates(_ctx(0), set(), 0)
    assert [i.symbol for i in intents] == ["ETHTMN", "BTCTMN", "USDTTMN"]
    assert len({i.group for i in intents}) == 1 and all(i.limit_at_bid and i.reprices == 1 for i in intents)
    top = intents[0]
    assert abs(top.take_profit / top.limit_price - 1.008) < 1e-9 and abs(top.stop_price / top.limit_price - 0.99) < 1e-9
    assert top.entry_timeout_minutes == 3.0


def test_slot_spacing_quota_and_concurrency_gate_the_candidates():
    module = AIActivityModule(AIActivityParams(max_per_day=2))
    assert module.candidates(_ctx(0), set(), 0)
    assert module.candidates(_ctx(6), set(), 0) == []  # 30 min: same slot
    module.note_entry(CLOCK[0])
    assert module.candidates(_ctx(12), set(), 0)  # 60 min: new slot, spacing 45 min passed
    module.note_entry(CLOCK[12])
    assert module.candidates(_ctx(24), set(), 0) == []  # daily quota of 2 used
    other = AIActivityModule()
    assert other.candidates(_ctx(0), set(), 2) == []  # two positions open
    assert [i.symbol for i in other.candidates(_ctx(12), {"ETHTMN"}, 1)] == ["BTCTMN", "USDTTMN"]


def test_max_hold_exits_with_a_maker_limit():
    module = AIActivityModule()

    class Held:
        opened_index = 0

    assert module.manage(_ctx(23), "BTCTMN", Held()) == []
    (exit_intent,) = module.manage(_ctx(24), "BTCTMN", Held())
    assert exit_intent.kind == "EXIT" and exit_intent.exit_type == "maker"
