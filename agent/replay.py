"""LLM clients by mode, the agent layer for backtests, and the resumable officer precompute.

Modes (`--llm`):
  * "replay"      — cache only, under the pinned local model's identity; never calls anything.
                    This is what `make reproduce` uses: it needs no model installed.
  * "live-local"  — the pinned local Ollama model (digest checked); answers are written to the cache.
  * "live"        — shadow/live: local model first, then the optional free-tier fallbacks (.env keys).
                    Never used by the canonical backtest.

The officer's historical calls are path-independent (market-only context), so `precompute_officer`
runs them once, in time order, with a progress log; it is resumable because every answer is cached.
"""

from __future__ import annotations

import json
import logging
import os
import time
import tomllib
from pathlib import Path

import pandas as pd

from strategy.base import Context

from .llm import (
    Budget,
    Identity,
    LLMClient,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderError,
    ReplayProvider,
    ResponseCache,
)
from .roles import AgentLayer, RegimeOfficer, TradeAgent, market_context

logger = logging.getLogger(__name__)

PROJECT = Path(__file__).resolve().parent.parent
CONFIG = PROJECT / "config" / "llm.toml"
CACHE_DIR = Path(os.environ.get("LLM_CACHE_DIR", PROJECT / "data" / "llm_cache"))  # committed: replay needs no model
JOURNAL = PROJECT / "logs" / "llm_journal.jsonl"
TRADED = ["USDTTMN", "BTCTMN", "ETHTMN"]


def load_llm_config() -> dict:
    return tomllib.loads(CONFIG.read_text(encoding="utf-8"))


def pinned_identity(config: dict | None = None) -> Identity:
    local = (config or load_llm_config())["local"]
    return Identity(local["provider"], local["model"], local["digest"])


def providers_for(mode: str, config: dict | None = None) -> list:
    config = config or load_llm_config()
    local = config["local"]
    if mode == "replay":
        return [ReplayProvider(pinned_identity(config))]
    providers = [OllamaProvider(local["model"], local["base_url"], local["digest"], num_ctx=int(local["num_ctx"]))]
    if mode == "live":
        for name, spec in config.get("fallbacks", {}).items():
            try:
                providers.append(
                    OpenAICompatibleProvider(name, spec["base_url"], os.environ.get(spec["key_env"], ""), spec["model"])
                )
            except ProviderError as exc:
                logger.info("fallback %s not configured: %s", name, exc)
    elif mode != "live-local":
        raise ValueError(f"unknown LLM mode {mode!r}: replay, live-local or live")
    return providers


def make_client(mode: str, config: dict | None = None) -> LLMClient:
    config = config or load_llm_config()
    budget = Budget(daily_tokens=int(os.environ.get("LLM_DAILY_TOKEN_BUDGET", "5000000")))
    return LLMClient(
        providers_for(mode, config),
        ResponseCache(CACHE_DIR),
        JOURNAL,
        budget=budget,
        options=dict(config.get("options", {})),
    )


def build_agent_layer(spec: dict, symbols: list[str]) -> AgentLayer:
    """spec = {"officer": bool, "trade_agent": bool, "llm": "replay"|"live-local"}. Historical runs are
    blind, with no order book tools (the recorder starts 2026-10-22), and the officer runs every 4 h."""
    config = load_llm_config()
    client = make_client(spec.get("llm", "replay"), config)
    officer = RegimeOfficer(client) if spec.get("officer") else None
    agent = TradeAgent(client) if spec.get("trade_agent") else None
    hours = int(config["cadence"]["officer_hours_replay"])
    return AgentLayer(
        officer, agent, [s for s in TRADED if s in symbols], blind=True, tools_factory=None, officer_hours=hours
    )


def officer_ticks(data, starts: list[pd.Timestamp], window_days: float, hours: int) -> list[int]:
    """Clock indexes where the officer decides: the first tick of every `hours` block inside the windows."""
    ticks = set()
    for start in starts:
        end = start + pd.Timedelta(days=window_days)
        for block in pd.date_range(start.floor(f"{hours}h"), end, freq=f"{hours}h", inclusive="left"):
            moment = max(block, start)
            index = int(data.clock.searchsorted(moment))
            if index < len(data.clock):
                ticks.add(index)
    return sorted(ticks)


def officer_plan(data, starts, window_days: float, client: LLMClient) -> dict:
    hours = int(load_llm_config()["cadence"]["officer_hours_replay"])
    officer = RegimeOfficer(client)
    ctx = Context(data.clock, data.features, data.symbols)
    symbols = [s for s in TRADED if s in data.features]
    todo = []
    ticks = officer_ticks(data, starts, window_days, hours)
    for index in ticks:
        ctx.index = index
        if not client.is_cached(officer.first_request(market_context(ctx, symbols, blind=True))):
            todo.append(index)
    return {"officer_calls": len(ticks), "cached": len(ticks) - len(todo), "missing": todo, "hours": hours}


def precompute_officer(data, ticks: list[int], client: LLMClient, progress: Path, log_every: int = 10) -> dict:
    """Run the officer at `ticks` in time order; resumable (cached answers are skipped instantly)."""
    officer = RegimeOfficer(client)
    ctx = Context(data.clock, data.features, data.symbols)
    symbols = [s for s in TRADED if s in data.features]
    started = time.monotonic()
    done = fallbacks = 0
    progress.parent.mkdir(parents=True, exist_ok=True)
    for number, index in enumerate(ticks, start=1):
        ctx.index = index
        outcome = officer.decide(market_context(ctx, symbols, blind=True))
        done += 1
        fallbacks += not outcome.ok
        if number % log_every == 0 or number == len(ticks):
            elapsed = time.monotonic() - started
            line = {
                "done": number,
                "total": len(ticks),
                "fallbacks": fallbacks,
                "elapsed_s": round(elapsed),
                "eta_s": round(elapsed / number * (len(ticks) - number)),
            }
            with progress.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")
            logger.info("officer precompute %s", line)
    return {"calls": done, "fallbacks": fallbacks, "seconds": round(time.monotonic() - started)}
