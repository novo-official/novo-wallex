"""Phase 4 preview: the context the LLM Regime/Risk Officer and Trade Agent will receive.

Not wired to any LLM yet. The interfaces are fixed now so that a news feed can be plugged in
later without touching the strategy: the strongest real role found so far for an LLM is macro /
news risk for USDTTMN (the rial moves on political and economic news, which price data cannot
anticipate), plus approving or vetoing rule entries.

Everything here is read-only and JSON-serializable; prompts are built from `build()`'s output.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from strategy.base import Context, PortfolioView

SCHEMA_VERSION = "context/1"


@dataclass(frozen=True)
class NewsItem:
    published_at: datetime
    source: str
    title: str
    summary: str = ""
    language: str = "fa"
    url: str | None = None
    tags: tuple[str, ...] = ()


class NewsFeed(Protocol):
    """Anything that returns news items published in [since, until)."""

    def items(self, since: datetime, until: datetime) -> list[NewsItem]: ...


class NullNewsFeed:
    """Default: no news. The context says so explicitly, so the LLM never assumes calm."""

    def items(self, since: datetime, until: datetime) -> list[NewsItem]:
        return []


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol: str
    close: float
    ret_1h: float
    ret_1d: float
    vol_1d: float
    uptrend_1h: bool
    fv_premium: float | None
    ml_edge: float | None = None


@dataclass
class ContextBuilder:
    symbols: list[str]
    news: NewsFeed = field(default_factory=NullNewsFeed)
    news_lookback: timedelta = timedelta(hours=24)
    ml_column: str | None = None

    def _snapshot(self, ctx: Context, symbol: str) -> SymbolSnapshot:
        def value(column: str) -> float | None:
            if column not in ctx.features[symbol]:
                return None
            number = ctx.value(symbol, column)
            return None if not math.isfinite(number) else round(number, 6)

        close = value("close") or 0.0
        uptrend = (
            (value("close_1h") or 0) > (value("ema200_1h") or math.inf)
            and (value("ema50_1h") or 0) > (value("ema200_1h") or math.inf)
            and (value("ema50_slope_1h") or 0) > 0
        )
        previous_day = ctx.value(symbol, "close", lag=288)
        return SymbolSnapshot(
            symbol=symbol,
            close=close,
            ret_1h=value("ret_1h") or 0.0,
            ret_1d=round(math.log(close / previous_day), 6) if close and math.isfinite(previous_day) else 0.0,
            vol_1d=value("vol_1d") or 0.0,
            uptrend_1h=bool(uptrend),
            fv_premium=value("fv_premium"),
            ml_edge=value(self.ml_column) if self.ml_column else None,
        )

    def build(self, ctx: Context, portfolio: PortfolioView, initial_equity: float) -> dict:
        now = ctx.now.to_pydatetime()
        news = self.news.items(now - self.news_lookback, now)
        snapshots = [self._snapshot(ctx, symbol) for symbol in self.symbols if symbol in ctx.features]
        equity = float(portfolio.equity)
        return {
            "schema": SCHEMA_VERSION,
            "as_of": now.isoformat(),
            "markets": [asdict(snapshot) for snapshot in snapshots],
            "breadth_uptrend_1h": sum(s.uptrend_1h for s in snapshots) / len(snapshots) if snapshots else None,
            "portfolio": {
                "equity_tmn": round(equity),
                "return_since_start": round(equity / initial_equity - 1, 6),
                "positions": sorted(portfolio.positions),
                "entries_allowed": portfolio.entries_allowed,
            },
            "news": {
                "available": not isinstance(self.news, NullNewsFeed),
                "lookback_hours": self.news_lookback.total_seconds() / 3600,
                "items": [
                    {**asdict(item), "published_at": item.published_at.isoformat(), "tags": list(item.tags)}
                    for item in news
                ],
            },
        }
