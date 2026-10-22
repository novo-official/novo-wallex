"""News for the Regime Officer: free sources only, summarized by the same local model. Interface + stub.

Status (user decision 2026-10-22): built as an interface only; it is evaluated in shadow, never in the
historical backtest (there is no news archive for replay, and the backtest context stays blind).

  * `RSSNewsFeed` — public RSS/Atom feeds (Iranian economic news, USDT and gold-rate news sites) listed in
    config/llm.toml [news].sources; the fetch function is injected, so tests and shadow runs control it.
    No source URL ships with the code: the team chooses and verifies them.
  * `NewsDigest` — the local model turns the last hours of headlines into a short, schema-validated
    digest (USDT pressure, gold, policy, sanctions, exchange incidents) that the officer receives.
  * `StubNewsFeed` — fixed items, for wiring tests and dry runs.
Headlines are data, never instructions: they enter the prompt as quoted JSON strings, and the digest
schema has no field that could change trading parameters.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

from pydantic import BaseModel

from .context import NewsItem
from .llm import LLMClient
from .prompts import Prompt, load_prompt


class StubNewsFeed:
    def __init__(self, items: list[NewsItem]) -> None:
        self._items = items

    def items(self, since: datetime, until: datetime) -> list[NewsItem]:
        return [item for item in self._items if since <= item.published_at < until]


def parse_feed(xml_text: str, source: str) -> list[NewsItem]:
    """RSS 2.0 <item> or Atom <entry>; unparseable dates are skipped (we never guess a time)."""
    root = ElementTree.fromstring(xml_text)
    items = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in node}
        stamp = fields.get("pubDate") or fields.get("published") or fields.get("updated")
        try:
            published = parsedate_to_datetime(stamp) if stamp and "," in stamp else datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            continue
        items.append(
            NewsItem(
                published_at=published.astimezone(UTC),
                source=source,
                title=fields.get("title", "")[:300],
                summary=(fields.get("description") or fields.get("summary") or "")[:600],
            )
        )
    return items


class RSSNewsFeed:
    def __init__(self, sources: dict[str, str], fetch: Callable[[str], str]) -> None:
        self.sources = sources  # name -> URL, from config/llm.toml
        self.fetch = fetch

    def items(self, since: datetime, until: datetime) -> list[NewsItem]:
        found: list[NewsItem] = []
        for name, url in self.sources.items():
            try:
                found += parse_feed(self.fetch(url), name)
            except Exception:  # noqa: BLE001 - one broken feed must not stop the others
                continue
        return sorted((i for i in found if since <= i.published_at < until), key=lambda i: i.published_at)


class NewsDigestOutput(BaseModel):
    usdt_pressure: Literal["down", "neutral", "up", "unclear"]
    shock_risk: Literal["low", "elevated", "high"]
    topics: list[
        Literal["fx", "gold", "monetary_policy", "sanctions", "politics", "exchange_incident", "crypto", "other"]
    ]
    summary: str


class NewsDigest:
    """Local-model digest of recent headlines for the officer's context."""

    def __init__(self, client: LLMClient, prompt: Prompt | None = None) -> None:
        self.client = client
        self.prompt = prompt or load_prompt("news_digest_v1")

    def digest(self, items: list[NewsItem]) -> dict:
        if not items:
            return {"available": True, "items": 0, "digest": None}
        headlines = [
            {"published_at": i.published_at.isoformat(), "source": i.source, "title": i.title, "summary": i.summary}
            for i in items[-40:]
        ]
        result = self.client.run(
            role="news_digest",
            prompt_version=self.prompt.version,
            system=self.prompt.text,
            user=json.dumps({"headlines": headlines}, ensure_ascii=False, sort_keys=True),
            output=NewsDigestOutput,
        )
        if not result.ok:
            return {"available": False, "items": len(items), "error": result.error}
        return {"available": True, "items": len(items), "digest": result.data.model_dump(), "provenance": result.tag}
