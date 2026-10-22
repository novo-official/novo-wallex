"""LLM client for the agent roles: one structured-output call with optional read-only tools.

Guarantees, in this order:
  * every request and response is cached on disk by the sha256 of the full request (model, system,
    messages, tools, schema, effort), so a replay is free and identical;
  * every call is journaled (role, prompt version, prompt hash, cache hit, tokens, outcome);
  * a daily token budget: when it is spent, calls are not made and the role falls back;
  * at most `max_tool_calls` tool calls; then the model must answer;
  * invalid output -> one retry with the validation error -> safe fallback (the caller's default).

Providers: `AnthropicProvider` (official SDK, `claude-opus-5-5` by default, its HTTP traffic goes
through LLM_HTTPS_PROXY and nothing else does), `ReplayProvider` (cache only: a miss is an error,
so a replay can never call the network), and test fakes. Opus 5.5 takes no `temperature`;
determinism for backtests comes from the cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output), for the budget report only.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


class ProviderError(Exception):
    """The provider could not produce a response (network, API error, cache miss in replay)."""


class Provider(Protocol):
    name: str

    def create(self, request: dict) -> dict:
        """Send one Messages API request (plain JSON) and return the response as plain JSON."""


class AnthropicProvider:
    """Claude through the official SDK. Server-side fallback on refusals is enabled by default."""

    name = "anthropic"

    def __init__(self, proxy: str | None = None, timeout: float = 120.0, fallbacks: bool = True) -> None:
        import anthropic  # lazy: the rest of the system works without the SDK installed

        http_client = anthropic.DefaultHttpxClient(proxy=proxy) if proxy else None
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(timeout=timeout, max_retries=2, http_client=http_client)
        self._fallbacks = fallbacks

    def create(self, request: dict) -> dict:
        anthropic = self._anthropic
        try:
            if self._fallbacks:
                response = self._client.beta.messages.create(
                    **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                response = self._client.messages.create(**request)
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"bad request: {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            raise ProviderError("authentication failed: check the API key") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("rate limited") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("connection error (is LLM_HTTPS_PROXY set and reachable?)") from exc
        return response.to_dict()


class ReplayProvider:
    """Never calls anything: every request must already be in the cache."""

    name = "replay"

    def create(self, request: dict) -> dict:
        raise ProviderError("replay mode: request not in the cache")


def request_hash(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class ResponseCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["response"]

    def put(self, key: str, request: dict, response: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"request": request, "response": response, "stored_at": datetime.now(UTC).isoformat()}
        path.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")


@dataclass
class Budget:
    daily_tokens: int
    used: dict[str, int] = field(default_factory=dict)

    def available(self, day: str) -> bool:
        return self.used.get(day, 0) < self.daily_tokens

    def spend(self, day: str, tokens: int) -> None:
        self.used[day] = self.used.get(day, 0) + tokens


@dataclass
class LLMResult:
    ok: bool
    data: BaseModel | None
    prompt_hash: str
    cached: bool
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


@dataclass
class ToolSet:
    """Read-only tools: JSON schemas for the API plus a dispatcher returning JSON-serializable results."""

    definitions: list[dict]
    call: Callable[[str, dict], object]


def strict_schema(model: type[BaseModel]) -> dict:
    """A JSON schema for output_config.format from a flat pydantic model (no $refs, all fields required)."""
    schema = model.model_json_schema()
    if "$defs" in schema:
        raise ValueError(f"{model.__name__}: nested models are not supported in the strict schema")

    def clean(node):
        if isinstance(node, dict):
            node = {k: clean(v) for k, v in node.items() if k not in ("title", "default")}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = sorted(node["properties"])
                node["additionalProperties"] = False
        elif isinstance(node, list):
            node = [clean(item) for item in node]
        return node

    return clean(schema)


class LLMClient:
    def __init__(
        self,
        provider: Provider,
        cache: ResponseCache,
        journal_path: Path,
        model: str = DEFAULT_MODEL,
        budget: Budget | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.journal_path = journal_path
        self.model = model
        self.budget = budget or Budget(daily_tokens=2_000_000)
        self.clock = clock

    def build_request(
        self, system: str, messages: list[dict], schema: dict, tools: ToolSet | None, effort: str, max_tokens: int
    ) -> dict:
        """The exact Messages API request; also used by the batch precompute, so hashes match."""
        request = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        }
        if tools is not None:
            request["tools"] = tools.definitions
        return request

    def _send(self, request: dict) -> tuple[dict, bool, str]:
        key = request_hash(request)
        cached = self.cache.get(key)
        if cached is not None:
            return cached, True, key
        day = self.clock().date().isoformat()
        if not self.budget.available(day):
            raise ProviderError("daily token budget spent")
        response = self.provider.create(request)
        usage = response.get("usage") or {}
        self.budget.spend(day, int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)))
        self.cache.put(key, request, response)
        return response, False, key

    def _journal(self, entry: dict) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": self.clock().isoformat(), **entry}, sort_keys=True, default=str) + "\n")

    def run(
        self,
        *,
        role: str,
        prompt_version: str,
        system: str,
        user: str,
        output: type[BaseModel],
        tools: ToolSet | None = None,
        max_tool_calls: int = 3,
        effort: str = "low",
        max_tokens: int = 4000,
    ) -> LLMResult:
        schema = strict_schema(output)
        messages: list[dict] = [{"role": "user", "content": user}]
        tool_calls = retries = input_tokens = output_tokens = 0
        all_cached = True
        first_hash = None
        error = None
        while True:
            request = self.build_request(system, messages, schema, tools, effort, max_tokens)
            try:
                response, cached, key = self._send(request)
            except ProviderError as exc:
                error = str(exc)
                break
            first_hash = first_hash or key
            all_cached &= cached
            usage = response.get("usage") or {}
            input_tokens += int(usage.get("input_tokens", 0))
            output_tokens += int(usage.get("output_tokens", 0))
            content = response.get("content") or []
            if response.get("stop_reason") == "refusal":
                error = "refusal"
                break
            uses = [block for block in content if block.get("type") == "tool_use"]
            if uses:
                messages = [*messages, {"role": "assistant", "content": content}]
                results = []
                for block in uses:
                    if tools is None or tool_calls >= max_tool_calls:
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block["id"],
                                "content": "Tool budget used up. Answer now with the JSON decision.",
                                "is_error": True,
                            }
                        )
                        continue
                    tool_calls += 1
                    try:
                        value = tools.call(block["name"], block.get("input") or {})
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block["id"],
                                "content": json.dumps(value, default=str),
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 - a tool failure is reported to the model, not raised
                        results.append(
                            {"type": "tool_result", "tool_use_id": block["id"], "content": str(exc), "is_error": True}
                        )
                messages = [*messages, {"role": "user", "content": results}]
                continue
            text = next((block.get("text") for block in content if block.get("type") == "text"), None)
            try:
                data = output.model_validate_json(text or "")
            except ValidationError as exc:
                if retries >= 1:
                    error = f"invalid output after retry: {exc.errors()[:2]}"
                    break
                retries += 1
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": f"That output did not validate: {exc.errors()[:3]}. Reply with valid JSON only.",
                    },
                ]
                continue
            result = LLMResult(True, data, first_hash, all_cached, tool_calls, input_tokens, output_tokens)
            self._journal(
                {
                    "role": role,
                    "prompt_version": prompt_version,
                    "prompt_hash": first_hash,
                    "cached": all_cached,
                    "tool_calls": tool_calls,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "ok": True,
                    "output": data.model_dump(),
                }
            )
            return result
        self._journal(
            {
                "role": role,
                "prompt_version": prompt_version,
                "prompt_hash": first_hash,
                "ok": False,
                "error": error,
                "tool_calls": tool_calls,
            }
        )
        logger.warning("%s: LLM call failed (%s); the caller falls back", role, error)
        return LLMResult(False, None, first_hash or "", all_cached, tool_calls, input_tokens, output_tokens, error)
