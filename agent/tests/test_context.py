import json
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from agent.context import ContextBuilder, NewsItem
from strategy.base import Context, FeatureFrame, PortfolioView

CLOCK = pd.date_range("2026-10-01T00:00:00Z", periods=400, freq="5min", tz="UTC")


def _ctx() -> Context:
    frame = pd.DataFrame(
        {
            "close": np.linspace(100, 110, len(CLOCK)),
            "close_1h": 109.0,
            "ema50_1h": 105.0,
            "ema200_1h": 100.0,
            "ema50_slope_1h": 0.001,
            "ret_1h": 0.002,
            "vol_1d": 0.001,
            "fv_premium": np.nan,
        },
        index=CLOCK,
    )
    ctx = Context(CLOCK, {"USDTTMN": FeatureFrame(frame)}, ["USDTTMN"])
    ctx.index = len(CLOCK) - 1
    return ctx


class FakeFeed:
    def items(self, since, until):
        return [NewsItem(published_at=until - timedelta(hours=1), source="test", title="rial falls", tags=("fx",))]


VIEW = PortfolioView(
    equity=Decimal("5050000"), cash=Decimal("5050000"), positions={}, pending_entries=frozenset(), entries_allowed=True
)


def test_context_is_json_and_says_when_no_news_feed_exists():
    payload = ContextBuilder(["USDTTMN"]).build(_ctx(), VIEW, 5_000_000)
    json.dumps(payload)
    assert payload["news"] == {"available": False, "lookback_hours": 24.0, "items": []}
    (market,) = payload["markets"]
    assert market["uptrend_1h"] is True and market["fv_premium"] is None
    assert payload["portfolio"]["return_since_start"] == 0.01


def test_a_news_feed_plugs_in_without_other_changes():
    payload = ContextBuilder(["USDTTMN"], news=FakeFeed()).build(_ctx(), VIEW, 5_000_000)
    assert payload["news"]["available"] is True
    assert payload["news"]["items"][0]["title"] == "rial falls"
    assert datetime.fromisoformat(payload["news"]["items"][0]["published_at"]) < CLOCK[-1].to_pydatetime()
