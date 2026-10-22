"""WallexTools: the read-only tools the Trade Agent may call (at most 3 per decision).

An adapter, so the official Wallex MCP server can replace it later without touching the agents:
  * LiveWallexTools      — GET /v1/depth, /v1/trades, /v1/markets through WallexClient, policy-checked;
  * RecordedWallexTools  — the recorder's Parquet files, as of a given time (shadow and recent replay);
  * UnavailableTools     — historical replay before the recorder existed: every call says "unavailable".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pyarrow.compute as pc

from market.orderbook_features import book_features
from market.parquet_store import read_hours

from .llm import ToolSet

SYMBOL = {"type": "string", "enum": ["USDTTMN", "BTCTMN", "ETHTMN"]}
DEFINITIONS = [
    {
        "name": "get_orderbook",
        "description": "Top of the order book of one market: spread, depth within 0.5% of mid, top-5 imbalance, "
        "and the best 5 levels per side.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"symbol": SYMBOL},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_recent_trades",
        "description": "The last public trades of one market (up to 50): count, buy share, volume and the "
        "price change across them.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"symbol": SYMBOL},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_market_info",
        "description": "Exchange rules of one market: price and amount decimals, minimum order value, whether "
        "market orders are enabled.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"symbol": SYMBOL},
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
]


def _summarize_book(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> dict:
    features = book_features(bids, asks)
    if features is None:
        return {"available": False, "reason": "empty or crossed book"}
    mid = (bids[0][0] + asks[0][0]) / 2
    return {
        "available": True,
        "spread_bps": round(features.spread_bps, 2),
        "imbalance_top5": round(features.imbalance_top5, 3),
        "bid_depth_tmn_within_0.5pct": round(features.bid_depth_50bps),
        "ask_depth_tmn_within_0.5pct": round(features.ask_depth_50bps),
        "levels_bps_from_mid": {
            "bids": [[round((p / mid - 1) * 1e4, 1), round(p * q)] for p, q in bids[:5]],
            "asks": [[round((p / mid - 1) * 1e4, 1), round(p * q)] for p, q in asks[:5]],
        },
    }


def _summarize_trades(trades: list[dict]) -> dict:
    if not trades:
        return {"available": True, "count": 0}
    prices = [float(t["price"]) for t in trades]
    return {
        "available": True,
        "count": len(trades),
        "buy_share": round(sum(bool(t["is_buy_order"]) for t in trades) / len(trades), 3),
        "volume_tmn": round(sum(float(t["price"]) * float(t["quantity"]) for t in trades)),
        "price_change_bps_oldest_to_newest": round((prices[0] / prices[-1] - 1) * 1e4, 1),
    }


class UnavailableTools:
    def call(self, name: str, arguments: dict) -> dict:
        return {"available": False, "reason": "no order book or trade history for this time (historical replay)"}

    def toolset(self) -> ToolSet:
        return ToolSet(DEFINITIONS, self.call)


class RecordedWallexTools:
    def __init__(self, orderbook_dir: Path, rules: dict, now: datetime) -> None:
        self.orderbook_dir = orderbook_dir
        self.rules = rules
        self.now = now

    def call(self, name: str, arguments: dict) -> dict:
        symbol = arguments["symbol"]
        if name == "get_market_info":
            return self.rules.get(symbol, {"available": False})
        stream = "depth" if name == "get_orderbook" else "trades"
        table = read_hours(self.orderbook_dir, stream, self.now - timedelta(hours=1), self.now + timedelta(seconds=1))
        if table is None:
            return {"available": False, "reason": "no recorded data near this time"}
        table = table.filter(pc.equal(table["symbol"], symbol))
        rows = [row for row in table.to_pylist() if row["captured_at"] <= self.now]
        if not rows:
            return {"available": False, "reason": "no recorded data near this time"}
        if name == "get_orderbook":
            last = max(row["captured_at"] for row in rows)
            book = [row for row in rows if row["captured_at"] == last]
            bids = [
                (float(r["price"]), float(r["quantity"]))
                for r in sorted(book, key=lambda r: r["level"])
                if r["side"] == "bid"
            ]
            asks = [
                (float(r["price"]), float(r["quantity"]))
                for r in sorted(book, key=lambda r: r["level"])
                if r["side"] == "ask"
            ]
            return {"as_of_seconds_ago": round((self.now - last).total_seconds()), **_summarize_book(bids, asks)}
        trades = sorted(rows, key=lambda r: r["trade_time"], reverse=True)[:50]
        return _summarize_trades(trades)

    def toolset(self) -> ToolSet:
        return ToolSet(DEFINITIONS, self.call)


class LiveWallexTools:
    """GET only, through the same WallexClient and market policy as everything else."""

    def __init__(self, client, policy, rules: dict) -> None:
        self.client = client
        self.policy = policy
        self.rules = rules

    def call(self, name: str, arguments: dict) -> dict:
        from exchange.schemas import SingleMarketDepthResponse, TradesResponse

        symbol = arguments["symbol"]
        self.policy.check_data(symbol)
        if name == "get_market_info":
            return self.rules.get(symbol, {"available": False})
        if name == "get_orderbook":
            book = SingleMarketDepthResponse.model_validate(self.client.get_depth(symbol)).result
            return _summarize_book(
                [(float(level.price), float(level.quantity)) for level in book.bid],
                [(float(level.price), float(level.quantity)) for level in book.ask],
            )
        trades = TradesResponse.model_validate(self.client.get_trades(symbol)).result.latest_trades
        return _summarize_trades(
            [
                {"price": t.price, "quantity": t.quantity, "is_buy_order": t.is_buy_order, "trade_time": t.timestamp}
                for t in trades
            ]
        )

    def toolset(self) -> ToolSet:
        return ToolSet(DEFINITIONS, self.call)
