"""Provider-neutral LLM client for the agent roles: one schema-validated decision, optional read-only tools.

Design choice (user, 2026-10-22): zero-cost, offline, pinned, reproducible AI. The primary provider is
a local open-weight model served by Ollama (Qwen, pinned by name + digest), called with a JSON schema
(`format`), temperature 0 and a fixed seed. Free-tier OpenAI-compatible endpoints (Groq, Gemini Flash)
are optional fallbacks for shadow/live only, never for the canonical backtest.

Guarantees:
  * the cache key is sha256 over the provider identity (provider, model, digest) + the full request
    (system prompt, messages, schema, tools, options), so a replay is free, identical, and can never
    mix answers from different models;
  * `ReplayProvider` answers only from the cache, under the identity of the pinned local model;
  * every call is journaled (role, prompt version, provider, model, digest, request hash, cache hit,
    latency, tokens, outcome);
  * at most `max_tool_calls` tool calls; invalid output gets one retry with the error and the schema
    enforced (no tools), then the caller's safe fallback; every provider failing -> safe fallback;
  * a daily token budget.

Internal message format (OpenAI-like): {"role": "user"|"assistant"|"tool", "content": str,
"tool_calls": [{"id", "name", "arguments"}] (assistant), "tool_call_id", "name" (tool)}.
Normalized response: {"text", "tool_calls", "stop_reason": "end"|"tool"|"length"|"refusal", "usage"}.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_OPTIONS = {"temperature": 0, "seed": 42}


class ProviderError(Exception):
    """The provider could not produce a response (network, API error, cache miss in replay, digest mismatch)."""


@dataclass(frozen=True)
class Identity:
    provider: str
    model: str
    digest: str = ""

    def as_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "digest": self.digest}


class Provider(Protocol):
    identity: Identity

    def create(self, request: dict) -> dict:
        """Send one neutral request, return a normalized response."""


def _tools_openai(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in tools
    ]


# --- Ollama (primary, local) -----------------------------------------------------------------------


class OllamaProvider:
    """Local model through Ollama's native /api/chat, with structured outputs via `format`."""

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        pinned_digest: str = "",
        timeout: float = 300.0,
        num_ctx: int = 8192,
    ) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self.num_ctx = num_ctx
        digest = self.installed_digest(model)
        if pinned_digest and digest != pinned_digest:
            raise ProviderError(f"{model}: installed digest {digest[:12]} != pinned {pinned_digest[:12]}")
        self.identity = Identity("ollama", model, digest)

    def installed_digest(self, model: str) -> str:
        try:
            tags = self._http.get("/api/tags").json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama is not reachable: {exc}") from exc
        for entry in tags.get("models", []):
            if model in (entry.get("name"), entry.get("model")):
                return entry.get("digest", "")
        raise ProviderError(f"model {model!r} is not installed: run `ollama pull {model}`")

    def create(self, request: dict) -> dict:
        messages = [{"role": "system", "content": request["system"]}]
        for message in request["messages"]:
            if message["role"] == "assistant":
                entry = {"role": "assistant", "content": message.get("content") or ""}
                if message.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"function": {"name": c["name"], "arguments": c["arguments"]}} for c in message["tool_calls"]
                    ]
                messages.append(entry)
            elif message["role"] == "tool":
                messages.append({"role": "tool", "content": message["content"], "tool_name": message["name"]})
            else:
                messages.append({"role": "user", "content": message["content"]})
        options = {**request["options"], "num_predict": request["max_tokens"], "num_ctx": self.num_ctx}
        body = {"model": self.identity.model, "messages": messages, "stream": False, "options": options, "think": False}
        if request.get("tools"):
            body["tools"] = _tools_openai(request["tools"])
        else:
            body["format"] = request["schema"]  # schema-constrained decoding
        try:
            response = self._http.post("/api/chat", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Ollama call failed: {exc}") from exc
        data = response.json()
        message = data.get("message") or {}
        calls = [
            {"id": f"call_{i}", "name": c["function"]["name"], "arguments": c["function"].get("arguments") or {}}
            for i, c in enumerate(message.get("tool_calls") or [])
        ]
        stop = "tool" if calls else ("length" if data.get("done_reason") == "length" else "end")
        return {
            "text": message.get("content"),
            "tool_calls": calls,
            "stop_reason": stop,
            "usage": {
                "input_tokens": data.get("prompt_eval_count", 0),
                "output_tokens": data.get("eval_count", 0),
                "seconds": data.get("total_duration", 0) / 1e9,
            },
        }


# --- OpenAI-compatible (optional fallbacks: Groq, Gemini Flash free tiers) --------------------------


class OpenAICompatibleProvider:
    """Any /chat/completions endpoint. Rate limits are respected with retry-after / exponential backoff."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ProviderError(f"{name}: no API key configured")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )
        self.identity = Identity(name, model, "")
        self.max_retries = max_retries
        self.sleep = sleep

    def create(self, request: dict) -> dict:
        messages = [{"role": "system", "content": request["system"]}]
        for message in request["messages"]:
            if message["role"] == "assistant":
                entry = {"role": "assistant", "content": message.get("content") or ""}
                if message.get("tool_calls"):
                    entry["tool_calls"] = [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                        }
                        for c in message["tool_calls"]
                    ]
                messages.append(entry)
            elif message["role"] == "tool":
                messages.append(
                    {"role": "tool", "tool_call_id": message["tool_call_id"], "content": message["content"]}
                )
            else:
                messages.append({"role": "user", "content": message["content"]})
        body = {
            "model": self.identity.model,
            "messages": messages,
            "max_tokens": request["max_tokens"],
            "temperature": request["options"].get("temperature", 0),
            "seed": request["options"].get("seed"),
        }
        if request.get("tools"):
            body["tools"] = _tools_openai(request["tools"])
        else:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "decision", "schema": request["schema"], "strict": True},
            }
        response = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                raise ProviderError(f"{self.identity.provider}: {exc}") from exc
            if response.status_code == 429 and attempt < self.max_retries:
                self.sleep(float(response.headers.get("retry-after", 2 ** (attempt + 1))))
                continue
            break
        if response.status_code >= 400:
            raise ProviderError(f"{self.identity.provider}: HTTP {response.status_code}")
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = [
            {"id": c["id"], "name": c["function"]["name"], "arguments": json.loads(c["function"]["arguments"] or "{}")}
            for c in message.get("tool_calls") or []
        ]
        reason = choice.get("finish_reason")
        stop = "tool" if calls else {"length": "length", "content_filter": "refusal"}.get(reason, "end")
        usage = data.get("usage") or {}
        return {
            "text": message.get("content"),
            "tool_calls": calls,
            "stop_reason": stop,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }


class ReplayProvider:
    """Answers only from the cache, under the identity of the model that produced the answers."""

    def __init__(self, identity: Identity) -> None:
        self.identity = identity

    def create(self, request: dict) -> dict:
        raise ProviderError("replay mode: request not in the cache")


# --- cache, budget, client -------------------------------------------------------------------------


def request_hash(identity: Identity, request: dict) -> str:
    payload = {"identity": identity.as_dict(), "request": request}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


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

    def put(self, key: str, identity: Identity, request: dict, response: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"identity": identity.as_dict(), "request": request, "response": response}
        path.write_text(json.dumps(payload, sort_keys=True, default=str, indent=1) + "\n", encoding="utf-8")


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
    prompt_hash: str  # request hash of the first call (provider identity included)
    cached: bool
    identity: Identity | None = None
    prompt_version: str = ""
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def tag(self) -> str:
        """Provenance for decision logs: provider/model@digest prompt-version #hash."""
        if self.identity is None:
            return f"{self.prompt_version} #{self.prompt_hash[:12]}"
        digest = self.identity.digest[:12] or "-"
        return f"{self.identity.provider}/{self.identity.model}@{digest} {self.prompt_version} #{self.prompt_hash[:12]}"


@dataclass
class ToolSet:
    """Read-only tools: JSON schemas plus a dispatcher returning JSON-serializable results."""

    definitions: list[dict]
    call: Callable[[str, dict], object]


def strict_schema(model: type[BaseModel]) -> dict:
    """A JSON schema from a flat pydantic model (no $refs, all fields required, no extra fields)."""
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
        providers: list,
        cache: ResponseCache,
        journal_path: Path,
        budget: Budget | None = None,
        options: dict | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers
        self.cache = cache
        self.journal_path = journal_path
        self.budget = budget or Budget(daily_tokens=5_000_000)
        self.options = {**DEFAULT_OPTIONS, **(options or {})}
        self.clock = clock

    @property
    def identity(self) -> Identity:
        return self.providers[0].identity

    def build_request(
        self, system: str, messages: list[dict], schema: dict, tools: ToolSet | None, max_tokens: int = 1500
    ) -> dict:
        """The neutral request; the precompute path uses the same function, so hashes match."""
        request = {
            "system": system,
            "messages": messages,
            "schema": schema,
            "max_tokens": max_tokens,
            "options": dict(self.options),
        }
        if tools is not None:
            request["tools"] = tools.definitions
        return request

    def first_key(self, request: dict) -> str:
        return request_hash(self.identity, request)

    def is_cached(self, request: dict) -> bool:
        return any(self.cache.get(request_hash(p.identity, request)) is not None for p in self.providers)

    def _send(self, request: dict) -> tuple[dict, bool, str, Identity]:
        errors = []
        for provider in self.providers:
            key = request_hash(provider.identity, request)
            cached = self.cache.get(key)
            if cached is not None:
                return cached, True, key, provider.identity
            day = self.clock().date().isoformat()
            if not self.budget.available(day):
                raise ProviderError("daily token budget spent")
            try:
                started = time.monotonic()
                response = provider.create(request)
            except ProviderError as exc:
                errors.append(f"{provider.identity.provider}: {exc}")
                continue
            usage = response.setdefault("usage", {})
            usage.setdefault("seconds", time.monotonic() - started)
            self.budget.spend(day, int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)))
            self.cache.put(key, provider.identity, request, response)
            return response, False, key, provider.identity
        raise ProviderError("; ".join(errors) or "no provider answered")

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
        max_tokens: int = 1500,
    ) -> LLMResult:
        schema = strict_schema(output)
        messages: list[dict] = [{"role": "user", "content": user}]
        active_tools = tools
        tool_calls = retries = input_tokens = output_tokens = 0
        seconds = 0.0
        all_cached, first_hash, identity, error = True, None, None, None
        while True:
            request = self.build_request(system, messages, schema, active_tools, max_tokens)
            try:
                response, cached, key, identity = self._send(request)
            except ProviderError as exc:
                error = str(exc)
                break
            first_hash = first_hash or key
            all_cached &= cached
            usage = response.get("usage") or {}
            input_tokens += int(usage.get("input_tokens", 0))
            output_tokens += int(usage.get("output_tokens", 0))
            seconds += float(usage.get("seconds", 0.0))
            if response.get("stop_reason") == "refusal":
                error = "refusal"
                break
            calls = response.get("tool_calls") or []
            if calls and active_tools is not None:
                results = []
                for call in calls:
                    if tool_calls >= max_tool_calls:
                        content = "Tool budget used up. Answer now with the JSON decision."
                    else:
                        tool_calls += 1
                        try:
                            content = json.dumps(
                                active_tools.call(call["name"], call.get("arguments") or {}), default=str
                            )
                        except Exception as exc:  # noqa: BLE001 - reported to the model, not raised
                            content = f"tool error: {exc}"
                    results.append(
                        {"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": content}
                    )
                assistant = {"role": "assistant", "content": response.get("text") or "", "tool_calls": calls}
                messages = [*messages, assistant, *results]
                if tool_calls >= max_tool_calls:
                    active_tools = None  # the next turn is schema-constrained: it must answer
                continue
            try:
                data = output.model_validate_json(response.get("text") or "")
            except ValidationError as exc:
                if retries >= 1:
                    error = f"invalid output after retry: {exc.errors()[:2]}"
                    break
                retries += 1
                active_tools = None  # enforce the schema on the retry
                feedback = f"That output did not validate: {exc.errors()[:3]}. Reply with the JSON object only."
                messages = [
                    *messages,
                    {"role": "assistant", "content": response.get("text") or ""},
                    {"role": "user", "content": feedback},
                ]
                continue
            result = LLMResult(
                True,
                data,
                first_hash,
                all_cached,
                identity,
                prompt_version,
                tool_calls,
                input_tokens,
                output_tokens,
                seconds,
            )
            self._journal(
                {
                    "role": role,
                    "prompt_version": prompt_version,
                    **identity.as_dict(),
                    "prompt_hash": first_hash,
                    "cached": all_cached,
                    "tool_calls": tool_calls,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "seconds": round(seconds, 3),
                    "ok": True,
                    "output": data.model_dump(),
                }
            )
            return result
        who = identity.as_dict() if identity else self.identity.as_dict()
        self._journal(
            {
                "role": role,
                "prompt_version": prompt_version,
                **who,
                "prompt_hash": first_hash,
                "ok": False,
                "error": error,
                "tool_calls": tool_calls,
            }
        )
        logger.warning("%s: LLM call failed (%s); the caller falls back", role, error)
        return LLMResult(
            False,
            None,
            first_hash or "",
            all_cached,
            identity,
            prompt_version,
            tool_calls,
            input_tokens,
            output_tokens,
            seconds,
            error,
        )
