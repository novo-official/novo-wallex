"""The two LLM roles and the layer that connects them to the engine (backtest, shadow, live).

  * RegimeOfficer (hourly): risk_mode, markets to avoid, whether activity trades may run.
    Its input is market-only (no account state), so a decision depends only on the hour and can be
    cached and batch-precomputed; account risk (loss limits, kill switch) is RiskGuard's job in code.
  * TradeAgent (per core rule entry): approve/reject, smaller size, tighter stop, closer target,
    with up to 3 read-only WallexTools calls. Activity trades are not reviewed one by one (cost);
    the officer can switch them off for the hour.

Ceilings are enforced here and again in RiskGuard: the officer's mode can never exceed `max_mode`,
and on any LLM failure the fallback is the safe one (officer: OFF = no new entries, exits continue;
trade agent: reject).

Blind mode (historical replay): no dates, no price levels, and no free-text rule reasons (they
contain prices), so the model cannot recognize the period from memory and leak hindsight into a
backtest. Live mode adds the real time and, when a feed is plugged in, the news.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel

from risk.guard import LLMAdjustment

from .context import NewsFeed, NullNewsFeed
from .llm import LLMClient, ToolSet, strict_schema
from .prompts import Prompt, load_prompt

MODE_ORDER = {"OFF": 0, "DEFENSIVE": 1, "NORMAL": 2}
TRADED = ("USDTTMN", "BTCTMN", "ETHTMN")


class RegimeDecision(BaseModel):
    risk_mode: Literal["NORMAL", "DEFENSIVE", "OFF"]
    usdt_risk: Literal["low", "elevated", "high"]
    avoid_symbols: list[Literal["USDTTMN", "BTCTMN", "ETHTMN"]]
    activity_trades: bool
    rationale: str


class TradeReview(BaseModel):
    approve: bool
    size_fraction: float
    stop_fraction: float
    take_profit_fraction: float
    rationale: str


def _num(value: float, digits: int = 5) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None


def market_context(ctx, symbols: list[str], blind: bool, news: NewsFeed | None = None) -> dict:
    """Market-only summary at the current clock index (closed bars only)."""
    markets = []
    for symbol in symbols:
        if symbol not in ctx.features:
            continue

        def value(column: str, lag: int = 0) -> float | None:
            if column not in ctx.features[symbol]:
                return None
            return _num(ctx.value(symbol, column, lag))

        close = ctx.value(symbol, "close")
        uptrend = (
            (value("close_1h") or 0) > (value("ema200_1h") or math.inf)
            and (value("ema50_1h") or 0) > (value("ema200_1h") or math.inf)
            and (value("ema50_slope_1h") or 0) > 0
        )
        entry = {
            "symbol": symbol,
            "ret_1h": value("ret_1h"),
            "ret_4h": _num(math.log(close / ctx.value(symbol, "close", 48))) if ctx.index >= 48 else None,
            "ret_1d": _num(math.log(close / ctx.value(symbol, "close", 288))) if ctx.index >= 288 else None,
            "vol_1d": value("vol_1d"),
            "uptrend_1h": bool(uptrend),
            "close_vs_ema200_1h": _num(close / ctx.value(symbol, "ema200_1h") - 1) if value("ema200_1h") else None,
            "rsi14_1h": value("rsi14_1h", 0),
            "adx14_4h": value("adx14_4h"),
            "active_share_1d": value("active_share_1d"),
            "fair_value_premium": value("fv_premium"),
        }
        if not blind:
            entry["last_price"] = close
        markets.append(entry)
    local = ctx.now.tz_convert("Asia/Tehran")
    context = {
        "tehran_hour": int(local.hour),
        "weekday": local.day_name(),
        "markets": markets,
        "breadth_uptrend_1h": round(sum(m["uptrend_1h"] for m in markets) / len(markets), 3) if markets else None,
    }
    if not blind:
        context["as_of"] = ctx.now.isoformat()
        now = ctx.now.to_pydatetime()
        items = (news or NullNewsFeed()).items(now - timedelta(hours=24), now)
        context["news"] = {
            "available": not isinstance(news or NullNewsFeed(), NullNewsFeed),
            "items": [
                {"published_at": i.published_at.isoformat(), "source": i.source, "title": i.title, "summary": i.summary}
                for i in items
            ],
        }
    else:
        context["news"] = {"available": False, "items": []}
    return context


def candidate_context(intent) -> dict:
    """The rule entry without price levels: distances and ratios only."""
    limit = float(intent.limit_price)
    signals = intent.signals or {}
    close = float(signals.get("close", limit))
    out = {
        "symbol": intent.symbol,
        "module": intent.module,
        "stop_fraction": _num(1 - float(intent.stop_price) / limit),
        "take_profit_fraction": _num(float(intent.take_profit) / limit - 1) if intent.take_profit else None,
    }
    if "prior_high" in signals:
        out["breakout_strength"] = _num(close / float(signals["prior_high"]) - 1)
    if "atr14_15m" in signals:
        out["atr_15m_fraction"] = _num(float(signals["atr14_15m"]) / close)
    for name in ("ema50_slope_1h", "rsi14_15m_min_1h", "stop_distance"):
        if name in signals:
            out[name] = _num(signals[name])
    return out


@dataclass
class RegimeOutcome:
    risk_mode: str
    avoid: frozenset[str]
    activity_trades: bool
    rationale: str
    prompt_hash: str
    ok: bool


class RegimeOfficer:
    def __init__(
        self, client: LLMClient, prompt: Prompt | None = None, max_mode: str = "NORMAL", effort: str = "low"
    ) -> None:
        self.client = client
        self.prompt = prompt or load_prompt("regime_officer_v1")
        self.max_mode = max_mode
        self.effort = effort

    def first_request(self, context: dict) -> dict:
        """The request `decide` sends first (same hash), for the batch precompute."""
        user = json.dumps(context, sort_keys=True)
        messages = [{"role": "user", "content": user}]
        return self.client.build_request(
            self.prompt.text, messages, strict_schema(RegimeDecision), None, self.effort, 4000
        )

    def decide(self, context: dict) -> RegimeOutcome:
        result = self.client.run(
            role="regime_officer",
            prompt_version=self.prompt.version,
            system=self.prompt.text,
            user=json.dumps(context, sort_keys=True),
            output=RegimeDecision,
            effort=self.effort,
        )
        if not result.ok:
            return RegimeOutcome("OFF", frozenset(), False, f"fallback: {result.error}", result.prompt_hash, False)
        decision: RegimeDecision = result.data
        mode = decision.risk_mode if MODE_ORDER[decision.risk_mode] <= MODE_ORDER[self.max_mode] else self.max_mode
        return RegimeOutcome(
            mode,
            frozenset(decision.avoid_symbols),
            decision.activity_trades,
            decision.rationale,
            result.prompt_hash,
            True,
        )


class TradeAgent:
    def __init__(self, client: LLMClient, prompt: Prompt | None = None, effort: str = "low") -> None:
        self.client = client
        self.prompt = prompt or load_prompt("trade_agent_v1")
        self.effort = effort

    @staticmethod
    def payload(intent, market: dict, tools: ToolSet | None) -> dict:
        return {"candidate": candidate_context(intent), "market": market, "tools_available": tools is not None}

    def first_request(self, intent, market: dict, tools: ToolSet | None) -> dict:
        """The request `review` sends first (same hash), for the batch precompute."""
        messages = [{"role": "user", "content": json.dumps(self.payload(intent, market, tools), sort_keys=True)}]
        return self.client.build_request(
            self.prompt.text, messages, strict_schema(TradeReview), tools, self.effort, 4000
        )

    def review(self, intent, market: dict, tools: ToolSet | None) -> LLMAdjustment:
        payload = self.payload(intent, market, tools)
        result = self.client.run(
            role="trade_agent",
            prompt_version=self.prompt.version,
            system=self.prompt.text,
            user=json.dumps(payload, sort_keys=True),
            output=TradeReview,
            tools=tools,
            effort=self.effort,
        )
        if not result.ok:
            return LLMAdjustment(approve=False, rationale=f"fallback: {result.error}", prompt_hash=result.prompt_hash)
        review: TradeReview = result.data
        limit = float(intent.limit_price)
        return LLMAdjustment(
            approve=review.approve,
            size_fraction=min(1.0, max(0.0, review.size_fraction)) * float(intent.size_fraction),
            stop_price=limit * (1 - review.stop_fraction) if review.stop_fraction > 0 else None,
            take_profit=limit * (1 + review.take_profit_fraction) if review.take_profit_fraction > 0 else None,
            rationale=review.rationale,
            prompt_hash=result.prompt_hash,
        )


@dataclass
class AgentLayer:
    """The engine's `agent` hook. Officer once per hour; trade agent for core (trend) entries."""

    officer: RegimeOfficer | None
    trade_agent: TradeAgent | None
    symbols: list[str]
    blind: bool = True
    tools_factory: object = None  # callable(ctx) -> ToolSet | None
    news: NewsFeed | None = None
    reviewed_modules: tuple[str, ...] = ("trend",)
    log: list[dict] = field(default_factory=list)
    _hour = None
    _outcome: RegimeOutcome | None = None

    def _regime(self, ctx) -> RegimeOutcome | None:
        if self.officer is None:
            return None
        hour = ctx.now.floor("1h")
        if hour != self._hour:
            self._hour = hour
            self._outcome = self.officer.decide(market_context(ctx, self.symbols, self.blind, self.news))
            self.log.append(
                {
                    "time": ctx.now.isoformat(),
                    "role": "regime_officer",
                    "risk_mode": self._outcome.risk_mode,
                    "avoid": sorted(self._outcome.avoid),
                    "activity": self._outcome.activity_trades,
                    "ok": self._outcome.ok,
                    "prompt_hash": self._outcome.prompt_hash,
                    "rationale": self._outcome.rationale,
                }
            )
        return self._outcome

    def risk_mode(self, ctx, view) -> str:
        outcome = self._regime(ctx)
        return outcome.risk_mode if outcome else "NORMAL"

    def review(self, intent, ctx, view) -> LLMAdjustment | None:
        outcome = self._regime(ctx)
        if outcome is not None:
            if intent.symbol in outcome.avoid:
                return LLMAdjustment(
                    approve=False,
                    rationale=f"officer: avoid {intent.symbol} ({outcome.rationale})",
                    prompt_hash=outcome.prompt_hash,
                )
            if intent.module == "ml_signal" and not outcome.activity_trades:
                return LLMAdjustment(
                    approve=False, rationale="officer: activity trades off this hour", prompt_hash=outcome.prompt_hash
                )
        if self.trade_agent is None or intent.module not in self.reviewed_modules:
            return None
        tools = self.tools_factory(ctx) if self.tools_factory else None
        adjustment = self.trade_agent.review(intent, market_context(ctx, self.symbols, self.blind, self.news), tools)
        self.log.append(
            {
                "time": ctx.now.isoformat(),
                "role": "trade_agent",
                "symbol": intent.symbol,
                "approve": adjustment.approve,
                "size_fraction": adjustment.size_fraction,
                "prompt_hash": adjustment.prompt_hash,
                "rationale": adjustment.rationale,
            }
        )
        return adjustment
