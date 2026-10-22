import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from agent.llm import Budget, LLMClient, ReplayProvider, ResponseCache, ToolSet, strict_schema
from agent.roles import (
    AgentLayer,
    RegimeDecision,
    RegimeOfficer,
    TradeAgent,
    TradeReview,
    candidate_context,
    market_context,
)
from agent.tools import RecordedWallexTools, UnavailableTools
from backtest.costs import CostModel, Fees
from backtest.engine import Backtester, MarketData
from risk.guard import SymbolRules
from risk.limits import RiskLimits
from strategy.base import Context, FeatureFrame, Intent

SAMPLE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "orderbook_sample"


def text(payload: dict, stop: str = "end_turn") -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "stop_reason": stop,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def tool_use(name: str, symbol: str, tool_id: str) -> dict:
    return {
        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {"symbol": symbol}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }


class Scripted:
    name = "scripted"

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def create(self, request: dict) -> dict:
        self.requests.append(request)
        return self.responses.pop(0)


REGIME = {"risk_mode": "NORMAL", "usdt_risk": "low", "avoid_symbols": [], "activity_trades": True, "rationale": "calm"}
REVIEW = {
    "approve": True,
    "size_fraction": 0.5,
    "stop_fraction": 0.01,
    "take_profit_fraction": 0.0,
    "rationale": "book is deep",
}


def _client(tmp_path, provider, budget=None) -> LLMClient:
    return LLMClient(provider, ResponseCache(tmp_path / "cache"), tmp_path / "journal.jsonl", budget=budget)


def _run(client, output=RegimeDecision, tools=None, user="{}"):
    return client.run(role="test", prompt_version="v1", system="sys", user=user, output=output, tools=tools)


def test_strict_schema_requires_every_field_and_forbids_extras():
    schema = strict_schema(RegimeDecision)
    assert schema["additionalProperties"] is False
    assert schema["required"] == sorted(RegimeDecision.model_fields)
    assert schema["properties"]["risk_mode"]["enum"] == ["NORMAL", "DEFENSIVE", "OFF"]


def test_responses_are_cached_and_journaled(tmp_path):
    provider = Scripted([text(REGIME)])
    client = _client(tmp_path, provider)
    first, second = _run(client), _run(client)
    assert first.ok and second.ok and not first.cached and second.cached
    assert len(provider.requests) == 1  # the replay did not call the provider
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["prompt_hash"] == first.prompt_hash


def test_replay_mode_never_calls_out_and_falls_back(tmp_path):
    result = _run(_client(tmp_path, ReplayProvider()))
    assert not result.ok and "replay" in result.error


def test_invalid_output_gets_one_retry_then_fails(tmp_path):
    bad = {"content": [{"type": "text", "text": '{"risk_mode": "YOLO"}'}], "stop_reason": "end_turn", "usage": {}}
    assert _run(_client(tmp_path / "a", Scripted([bad, text(REGIME)]))).ok
    result = _run(_client(tmp_path / "b", Scripted([bad, bad])))
    assert not result.ok and "after retry" in result.error


def test_refusal_and_budget_fall_back(tmp_path):
    assert not _run(_client(tmp_path / "a", Scripted([text(REGIME, stop="refusal")]))).ok
    spent = Budget(daily_tokens=10, used={datetime.now(UTC).date().isoformat(): 10})
    result = _run(_client(tmp_path / "b", Scripted([text(REGIME)]), budget=spent))
    assert not result.ok and "budget" in result.error


def test_tool_calls_are_capped_at_three(tmp_path):
    calls = []
    tools = ToolSet([{"name": "get_orderbook"}], lambda name, args: calls.append(name) or {"ok": True})
    script = [tool_use("get_orderbook", "BTCTMN", f"t{i}") for i in range(4)] + [text(REVIEW)]
    provider = Scripted(script)
    result = _run(_client(tmp_path, provider), output=TradeReview, tools=tools)
    assert result.ok and result.tool_calls == 3 and len(calls) == 3
    last_results = provider.requests[-1]["messages"][-1]["content"]
    assert last_results[0]["is_error"] and "budget" in last_results[0]["content"]


def test_officer_ceiling_and_fallback(tmp_path):
    officer = RegimeOfficer(_client(tmp_path / "a", Scripted([text(REGIME)])), max_mode="DEFENSIVE")
    assert officer.decide({"markets": []}).risk_mode == "DEFENSIVE"
    failing = RegimeOfficer(_client(tmp_path / "b", ReplayProvider()))
    outcome = failing.decide({"markets": []})
    assert outcome.risk_mode == "OFF" and not outcome.ok  # safe fallback: no new entries


CLOCK = pd.date_range("2026-10-01T00:00:00Z", periods=400, freq="5min", tz="UTC")


def _ctx(index: int = 399) -> Context:
    frame = pd.DataFrame(
        {
            "close": 22_000_000_000.0,
            "open": 22_000_000_000.0,
            "high": 22_010_000_000.0,
            "low": 21_990_000_000.0,
            "volume": 1.0,
            "close_1h": 22e9,
            "ema50_1h": 21.9e9,
            "ema200_1h": 21.5e9,
            "ema50_slope_1h": 0.001,
            "ret_1h": 0.001,
            "vol_1d": 0.002,
            "rsi14_1h": 55.0,
            "adx14_4h": 20.0,
            "active_share_1d": 0.7,
        },
        index=CLOCK,
    )
    ctx = Context(CLOCK, {"BTCTMN": FeatureFrame(frame)}, ["BTCTMN"])
    ctx.index = index
    return ctx


def _intent(module="trend") -> Intent:
    return Intent(
        kind="ENTER",
        symbol="BTCTMN",
        module=module,
        reason="5m close 2.2e+10 broke 2.19e+10",
        signals={"close": 22e9, "prior_high": 21.9e9, "atr14_15m": 4e7},
        limit_price=22e9,
        stop_price=22e9 * 0.98,
        take_profit=22e9 * 1.06,
    )


def test_blind_context_has_no_dates_prices_or_free_text():
    blind = json.dumps(market_context(_ctx(), ["BTCTMN"], blind=True)) + json.dumps(candidate_context(_intent()))
    assert "2026" not in blind and "as_of" not in blind and "last_price" not in blind
    assert "22000000000" not in blind and "2.2e+10" not in blind and "broke" not in blind
    live = market_context(_ctx(), ["BTCTMN"], blind=False)
    assert live["as_of"].startswith("2026-10-02") and live["markets"][0]["last_price"] == 22e9


def test_trade_agent_maps_review_to_a_clamped_adjustment(tmp_path):
    agent = TradeAgent(_client(tmp_path, Scripted([text(REVIEW)])))
    adjustment = agent.review(_intent(), {}, None)
    assert adjustment.approve and adjustment.size_fraction == 0.5
    assert adjustment.stop_price == pytest.approx(22e9 * 0.99) and adjustment.take_profit is None
    assert adjustment.rationale == "book is deep" and adjustment.prompt_hash


def test_layer_calls_the_officer_once_per_hour_and_applies_its_vetoes(tmp_path):
    avoid = {**REGIME, "avoid_symbols": ["BTCTMN"], "activity_trades": False}
    officer = RegimeOfficer(_client(tmp_path, Scripted([text(REGIME), text(avoid)])))
    layer = AgentLayer(officer, None, ["BTCTMN"])
    for index in (384, 385, 386):  # same hour
        assert layer.risk_mode(_ctx(index), None) == "NORMAL"
    assert len([e for e in layer.log if e["role"] == "regime_officer"]) == 1
    layer.risk_mode(_ctx(396), None)  # next hour
    assert not layer.review(_intent(), _ctx(396), None).approve
    assert not layer.review(_intent("ml_signal"), _ctx(396), None).approve


def _market(bars) -> MarketData:
    clock = pd.date_range("2026-10-01T00:00:00Z", periods=len(bars), freq="5min", tz="UTC")
    frame = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=clock)
    frame["volume"] = 1.0
    return MarketData(
        clock, {"AAATMN": FeatureFrame(frame)}, {"AAATMN": SymbolRules(4, 0, Decimal("50000"))}, ["AAATMN"]
    )


class Once:
    name = "once"

    def on_cycle(self, ctx, portfolio):
        if ctx.index != 0:
            return []
        return [
            Intent(
                kind="ENTER",
                symbol="AAATMN",
                module="trend",
                reason="r",
                limit_price=1000.0,
                stop_price=985.0,
                take_profit=1100.0,
            )
        ]


class FixedAgent:
    def __init__(self, mode, adjustment=None):
        self.mode, self.adjustment = mode, adjustment

    def risk_mode(self, ctx, view):
        return self.mode

    def review(self, intent, ctx, view):
        return self.adjustment


@pytest.mark.parametrize(("mode", "trades"), [("NORMAL", 1), ("OFF", 0)])
def test_engine_obeys_the_agent_and_risk_guard_clamps_it(mode, trades):
    from risk.guard import LLMAdjustment

    bars = [(1005, 1006, 1004, 1005), (1005, 1006, 999, 1001)] + [(1001, 1002, 1000, 1001)] * 3
    limits = RiskLimits(no_entry_before_end_minutes=0, flatten_before_end_minutes=0)
    costs = CostModel(Fees(Decimal("0.0025"), Decimal("0.0035")), {}, Decimal(0))
    agent = FixedAgent(mode, LLMAdjustment(size_fraction=5.0, rationale="go big"))  # 5x is ignored
    data = _market(bars)
    result = Backtester(data, Once(), costs, limits, agent=agent).run(data.clock[0], data.clock[-1])
    assert len(result.trades) == trades
    if trades:
        assert result.trades[0].quantity * Decimal("1000") <= Decimal("1000000")
        assert "LLM: go big" in result.decisions[0]["risk"]


def test_recorded_tools_read_the_book_as_of_a_time():
    tools = RecordedWallexTools(SAMPLE, {}, datetime(2026, 10, 22, 8, 55, tzinfo=UTC))
    book = tools.call("get_orderbook", {"symbol": "BTCTMN"})
    assert book["available"] and 0 < book["spread_bps"] < 50 and book["as_of_seconds_ago"] < 60
    early = RecordedWallexTools(SAMPLE, {}, datetime(2026, 10, 21, 8, 0, tzinfo=UTC))
    assert not early.call("get_orderbook", {"symbol": "BTCTMN"})["available"]
    assert not UnavailableTools().call("get_orderbook", {"symbol": "BTCTMN"})["available"]


def test_first_request_hash_matches_what_run_sends(tmp_path):
    from agent.llm import request_hash

    provider = Scripted([text(REGIME)])
    officer = RegimeOfficer(_client(tmp_path, provider))
    context = market_context(_ctx(), ["BTCTMN"], blind=True)
    expected = request_hash(officer.first_request(context))
    officer.decide(context)
    assert request_hash(provider.requests[0]) == expected
