"""LLM replay for backtests: build the agent layer, precompute the officer's hourly calls in a batch,
check coverage, and estimate cost before anything is spent.

Flow (each step is a command; nothing here spends money without --confirm-spend):
  1. plan      — enumerate the officer's hourly requests for the windows, count what is cached,
                 and estimate the cost of the rest;
  2. submit    — send the missing ones as one Message Batch (50% price; no `fallbacks` on batches);
  3. collect   — write the batch results into the response cache under the same request hashes;
  4. the ablation then runs: the officer replays from the cache; the trade agent calls the API only
     for entries that actually happen (few) and caches them too.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from strategy.base import Context

from .llm import (
    DEFAULT_MODEL,
    PRICES,
    AnthropicProvider,
    Budget,
    LLMClient,
    ReplayProvider,
    ResponseCache,
    request_hash,
)
from .roles import AgentLayer, RegimeOfficer, TradeAgent, market_context

PROJECT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT / "data" / "llm_cache"
JOURNAL = PROJECT / "logs" / "llm_journal.jsonl"
TRADED = ["USDTTMN", "BTCTMN", "ETHTMN"]


def make_client(mode: str) -> LLMClient:
    """mode: "replay" (cache only, never calls out) or "live" (Anthropic API through LLM_HTTPS_PROXY)."""
    provider = ReplayProvider() if mode == "replay" else AnthropicProvider(proxy=os.environ.get("LLM_HTTPS_PROXY"))
    budget = Budget(daily_tokens=int(os.environ.get("LLM_DAILY_TOKEN_BUDGET", "3000000")))
    return LLMClient(
        provider, ResponseCache(CACHE_DIR), JOURNAL, model=os.environ.get("LLM_MODEL", DEFAULT_MODEL), budget=budget
    )


def build_agent_layer(spec: dict, symbols: list[str]) -> AgentLayer:
    """spec = {"officer": bool, "trade_agent": bool, "officer_mode": "replay", "agent_mode": "live"|"replay"}.

    Historical runs are blind and have no order book tools (the recorder starts 2026-10-22)."""
    officer = RegimeOfficer(make_client(spec.get("officer_mode", "replay"))) if spec.get("officer") else None
    agent = TradeAgent(make_client(spec.get("agent_mode", "replay"))) if spec.get("trade_agent") else None
    # No tools in historical replay: the recorder has no order book before 2026-10-22, and a single-turn
    # request keeps the trade agent's calls cheap and cacheable.
    return AgentLayer(officer, agent, [s for s in TRADED if s in symbols], blind=True, tools_factory=None)


def officer_requests(data, starts: list[pd.Timestamp], window_days: float) -> dict[str, dict]:
    """Every request the officer will send in these windows: one per hour, at the hour's first tick."""
    officer = RegimeOfficer(make_client("replay"))
    ctx = Context(data.clock, data.features, data.symbols)
    symbols = [s for s in TRADED if s in data.features]
    requests: dict[str, dict] = {}
    for start in starts:
        end = start + pd.Timedelta(days=window_days)
        for hour in pd.date_range(start, end, freq="1h", inclusive="left"):
            index = int(data.clock.searchsorted(hour))
            if index >= len(data.clock):
                continue
            ctx.index = index
            request = officer.first_request(market_context(ctx, symbols, blind=True))
            requests[request_hash(request)] = request
    return requests


def estimate(
    requests: dict[str, dict], model: str = DEFAULT_MODEL, batch: bool = True, output_tokens: int = 400
) -> dict:
    """Rough cost: ~3.5 characters per token for the input; `output_tokens` per call (JSON + low-effort thinking)."""
    price_in, price_out = PRICES.get(model, PRICES[DEFAULT_MODEL])
    chars = sum(len(json.dumps(r)) for r in requests.values())
    input_tokens = chars / 3.5
    cost = input_tokens / 1e6 * price_in + len(requests) * output_tokens / 1e6 * price_out
    return {
        "requests": len(requests),
        "input_tokens_est": int(input_tokens),
        "output_tokens_est": len(requests) * output_tokens,
        "usd_est": round(cost * (0.5 if batch else 1), 2),
        "model": model,
        "batch": batch,
    }


def missing(requests: dict[str, dict]) -> dict[str, dict]:
    cache = ResponseCache(CACHE_DIR)
    return {key: request for key, request in requests.items() if cache.get(key) is None}


def submit_batch(requests: dict[str, dict], record: Path) -> str:
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    proxy = os.environ.get("LLM_HTTPS_PROXY")
    client = anthropic.Anthropic(http_client=anthropic.DefaultHttpxClient(proxy=proxy) if proxy else None)
    batch = client.messages.batches.create(
        requests=[
            Request(custom_id=key, params=MessageCreateParamsNonStreaming(**request))
            for key, request in requests.items()
        ]
    )
    record.parent.mkdir(parents=True, exist_ok=True)
    with record.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"batch_id": batch.id, "requests": len(requests)}) + "\n")
    return batch.id


def collect_batch(batch_id: str, requests: dict[str, dict]) -> dict:
    import anthropic

    proxy = os.environ.get("LLM_HTTPS_PROXY")
    client = anthropic.Anthropic(http_client=anthropic.DefaultHttpxClient(proxy=proxy) if proxy else None)
    status = client.messages.batches.retrieve(batch_id)
    if status.processing_status != "ended":
        return {"status": status.processing_status}
    cache = ResponseCache(CACHE_DIR)
    counts = {"succeeded": 0, "errored": 0, "canceled": 0, "expired": 0, "unknown_id": 0}
    for result in client.messages.batches.results(batch_id):
        kind = result.result.type
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "succeeded":
            if result.custom_id not in requests:
                counts["unknown_id"] += 1
                continue
            cache.put(result.custom_id, requests[result.custom_id], result.result.message.to_dict())
    return counts
