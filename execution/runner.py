"""Builds the engine for shadow or live from config/strategy.toml (canonical rules), config/live.toml
(risk profile, clock, activity, LLM) and config/llm.toml (pinned local model)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings

from backtest.config import load_study_config
from exchange.client import WallexClient
from exchange.factory import build_policy
from exchange.ratelimit import RateLimiter
from risk.guard import RiskGuard
from risk.limits import RiskLimits
from strategy.factory import build_strategy

from .broker import LiveBroker, ShadowBroker
from .engine import Engine, EngineSettings
from .feed import MarketFeed
from .journal import Journal

TEHRAN = ZoneInfo("Asia/Tehran")
TRADED = ["USDTTMN", "BTCTMN", "ETHTMN"]


def load_live_config() -> dict:
    return tomllib.loads((settings.BASE_DIR / "config" / "live.toml").read_text(encoding="utf-8"))


def challenge_end(live: dict) -> datetime | None:
    raw = live["challenge"]["end"].strip()
    if not raw:
        return None
    moment = datetime.fromisoformat(raw)
    return moment if moment.tzinfo else moment.replace(tzinfo=TEHRAN)


def live_limits(live: dict) -> RiskLimits:
    risk = live["risk"]
    return RiskLimits(
        max_position_fraction=Decimal(risk["max_position_fraction"]),
        max_total_exposure=Decimal(risk["max_total_exposure"]),
        kill_switch=Decimal(risk["kill_switch"]),
        daily_loss_limit=Decimal(risk["daily_loss_limit"]),
        min_order_tmn=Decimal(risk["min_order_tmn"]),
        no_entry_before_end_minutes=int(risk["no_entry_before_end_minutes"]),
        flatten_before_end_minutes=int(risk["flatten_before_end_minutes"]),
    )


def live_strategy_params(study, live: dict) -> dict:
    params = json.loads(json.dumps(study.strategy))
    ai = live.get("ai_activity", {})
    if ai.get("enabled"):
        # User decision 2026-10-22: trend OFF in live; every trade is an ML-ranked, LLM-approved AI activity trade.
        cap = Decimal(live["challenge"]["start_equity_tmn"]) * Decimal(live["risk"]["max_position_fraction"])
        notional = max(Decimal(ai["notional_tmn"]), Decimal(live["risk"]["min_order_tmn"]))
        params["modules"] = ["ai_activity"]
        params["ai_activity"] = {key: ai[key] for key in ai if key not in ("enabled", "notional_tmn")} | {
            "size_fraction": float(notional / cap)
        }
        return params
    activity = live["activity"]
    if activity.get("enabled"):
        cap = Decimal(live["challenge"]["start_equity_tmn"]) * Decimal(live["risk"]["max_position_fraction"])
        params["modules"] = ["trend", "ml_signal"]
        params["ml_signal"] = {
            "column": activity["column"],
            "threshold": activity["threshold"],
            "hold_bars": activity["hold_bars"],
            "stop": activity["stop"],
            "min_fraction": 1.0,
            "size_scale": float(Decimal(activity["notional_tmn"]) / cap),
            "max_entries_per_day": int(activity["max_entries_per_day"]),
        }
    return params


def code_version() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=settings.BASE_DIR).stdout
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "execution", "strategy", "risk", "agent", "config"],
            capture_output=True,
            text=True,
            cwd=settings.BASE_DIR,
        ).stdout.strip()
        return sha.strip()[:12] + ("-dirty" if dirty else "")
    except OSError:
        return "unknown"


def live_config_hash(study, live: dict) -> str:
    llm = (settings.BASE_DIR / "config" / "llm.toml").read_text(encoding="utf-8")
    payload = {
        "strategy": live_strategy_params(study, live),
        "live": live,
        "llm": llm,
        "slippage": {k: str(v) for k, v in study.slippage.items()},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def build_engine(mode: str) -> Engine:
    if mode not in ("shadow", "live"):
        raise ValueError("mode must be shadow or live")
    study = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
    live = load_live_config()
    policy = build_policy()
    key = settings.WALLEX_API_KEY if mode == "live" else ""
    client = WallexClient(
        settings.WALLEX_BASE_URL,
        key,
        timeout_seconds=settings.WALLEX_TIMEOUT_SECONDS,
        max_retries=settings.WALLEX_MAX_RETRIES,
        rate_limiter=RateLimiter(4.0),
    )
    data_symbols = list(study.tradable) + [s.removesuffix("TMN") + "USDT" for s in study.tradable if s != "USDTTMN"]
    feed = MarketFeed(
        client, policy, settings.BASE_DIR / "data", data_symbols, TRADED, int(live["engine"]["history_days"])
    )
    config_hash = live_config_hash(study, live) + "+" + code_version()
    journal = Journal(settings.BASE_DIR / "journal" / mode, mode, config_hash)
    start = Decimal(live["challenge"]["start_equity_tmn"])
    broker = LiveBroker(client, policy, journal) if mode == "live" else ShadowBroker(feed, study.fees, start)
    limits = live_limits(live)
    strategy = build_strategy(live_strategy_params(study, live), TRADED)
    for module in strategy.modules:
        if module.name == "ml_signal":
            module.name = live["activity"]["module_name"]  # decision logs show "activity"
    strategy.by_name = {module.name: module for module in strategy.modules}
    ai_on = bool(live.get("ai_activity", {}).get("enabled"))
    if ai_on and not (live["llm"]["officer"] and live["llm"]["trade_agent"]):
        raise ValueError("ai_activity needs both LLM roles: every trade is an ML + LLM decision")
    extra = _ml_columns(study) if (ai_on or live["activity"].get("enabled")) else None
    agent = _agent(live, client, policy, feed) if (live["llm"]["officer"] or live["llm"]["trade_agent"]) else None
    engine_cfg = live["engine"]
    settings_obj = EngineSettings(
        mode=mode,
        challenge_end=challenge_end(live),
        loop_seconds=int(engine_cfg["loop_seconds"]),
        reconcile_seconds=int(engine_cfg["reconcile_seconds"]),
        candle_refresh_seconds=int(engine_cfg["candle_refresh_seconds"]),
        entry_timeout_minutes=int(engine_cfg["entry_timeout_minutes"]),
        flatten_limit_timeout_seconds=int(engine_cfg["flatten_limit_timeout_seconds"]),
        max_consecutive_errors=int(engine_cfg["max_consecutive_errors"]),
        ramp_hours=float(live["risk"]["ramp_hours"]),
        ramp_factor=Decimal(live["risk"]["ramp_factor"]),
        taker_fee=study.fees.taker,
        start_equity=start,
    )
    engine = Engine(
        settings_obj,
        feed,
        broker,
        RiskGuard(limits, policy),
        strategy,
        journal,
        settings.BASE_DIR / "logs",
        agent=agent,
        extra_columns=extra,
    )
    engine.config_hash = config_hash
    return engine


def _ml_columns(study):
    from ml.dataset import ml_frames
    from ml.live import LivePredictor

    predictor = LivePredictor(settings.BASE_DIR / "models" / "ml_net_h24")

    def columns(features: dict) -> dict:
        frames = ml_frames(features, list(study.tradable), list(study.tradable))
        predictions = predictor.predict_frames(frames, [s for s in TRADED if s in frames])
        return {symbol: {"ml_net_h24": series} for symbol, series in predictions.items()}

    return columns


def _agent(live: dict, client, policy, feed):
    from agent.replay import make_client
    from agent.roles import AgentLayer, RegimeOfficer, TradeAgent
    from agent.tools import LiveWallexTools

    llm = make_client(live["llm"]["mode"])
    rules = {
        s: {
            "amount_decimals": r.amount_decimals,
            "price_decimals": r.price_decimals,
            "min_notional_tmn": str(r.min_notional),
        }
        for s, r in feed.rules.items()
        if s in TRADED
    }
    tools = LiveWallexTools(client, policy, rules)
    return AgentLayer(
        RegimeOfficer(llm) if live["llm"]["officer"] else None,
        TradeAgent(llm) if live["llm"]["trade_agent"] else None,
        TRADED,
        blind=False,
        tools_factory=lambda ctx: tools.toolset(),
        officer_hours=1,
    )


__all__ = ["build_engine", "load_live_config", "challenge_end", "live_limits", "replace"]
