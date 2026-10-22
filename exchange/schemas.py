"""Typed models for the Wallex responses this project uses.

Shapes come from docs/wallex_spot_swagger.json and were checked against real
captures in tests/fixtures/wallex/. Endpoints that are not in the Swagger file
(/v1/markets, /v1/depth, /v1/trades) are marked "probe-verified": their shapes
come only from real captures (2026-10-22, api.wallex.ir). Only the fields we use
are declared. Unknown keys are ignored, so new fields added by Wallex do not break a sync.

Numbers are Decimal: the client parses JSON numbers with parse_float=Decimal.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _blank_to_none(value: Any) -> Any:
    """Real captures send "", "-" or null for inactive markets (152 of 469 at capture time)."""
    return None if value in ("", "-", None) else value


OptionalDecimal = Annotated[Decimal | None, BeforeValidator(_blank_to_none)]


class WallexModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


# --- GET /hector/web/v1/markets ----------------------------------------------
# Price and volumes are optional: dead markets have "" or null in the real response.


class MarketInfo(WallexModel):
    symbol: str
    base_asset: str
    quote_asset: str
    is_spot: bool
    price: OptionalDecimal = None
    price_precision: int
    amount_precision: int
    volume_24h: OptionalDecimal = None
    quote_volume_24h: OptionalDecimal = None


class MarketsResult(WallexModel):
    markets: list[MarketInfo]


class MarketsResponse(WallexModel):
    success: bool
    message: str = ""
    result: MarketsResult


# --- GET /v1/udf/history (TradingView UDF format) ----------------------------
# Verified on a real capture: s == "ok", t in Unix seconds (bar open), and
# o/h/l/c/v as strings. Bars are aligned to the resolution boundary.


class UdfHistoryResponse(WallexModel):
    s: str  # "ok" or "no_data"
    t: list[int] = Field(default_factory=list)
    open_: list[Decimal] = Field(default_factory=list, alias="o")
    high: list[Decimal] = Field(default_factory=list, alias="h")
    low: list[Decimal] = Field(default_factory=list, alias="l")
    close: list[Decimal] = Field(default_factory=list, alias="c")
    volume: list[Decimal] = Field(default_factory=list, alias="v")

    @model_validator(mode="after")
    def check_equal_lengths(self) -> Self:
        lengths = {len(self.t), len(self.open_), len(self.high), len(self.low), len(self.close), len(self.volume)}
        if len(lengths) > 1:
            raise ValueError(f"UDF arrays have different lengths: {sorted(lengths)}")
        return self


# --- GET /v1/markets (probe-verified, not in Swagger) --------------------------
# Trading rules per market. stepSize and tickSize are decimal places (they equal
# amount_precision and price_precision of /hector/web/v1/markets for all 373
# markets in the capture), not increments. minNotional is in the quote asset.
# isMarketTypeEnable was true for only 7 of 373 markets: MARKET orders must not be
# assumed to exist.


class MarketStats(WallexModel):
    bid_price: OptionalDecimal = Field(default=None, alias="bidPrice")
    ask_price: OptionalDecimal = Field(default=None, alias="askPrice")
    last_price: OptionalDecimal = Field(default=None, alias="lastPrice")
    quote_volume_24h: OptionalDecimal = Field(default=None, alias="24h_quoteVolume")


class MarketRules(WallexModel):
    symbol: str
    base_asset: str = Field(alias="baseAsset")
    quote_asset: str = Field(alias="quoteAsset")
    amount_decimals: int = Field(alias="stepSize")
    price_decimals: int = Field(alias="tickSize")
    min_qty: Decimal = Field(alias="minQty")
    min_notional: Decimal = Field(alias="minNotional")
    market_orders_enabled: bool = Field(alias="isMarketTypeEnable")
    is_zero_fee: bool = Field(alias="isZeroFee")
    stats: MarketStats


class MarketRulesResult(WallexModel):
    symbols: dict[str, MarketRules]


class MarketRulesResponse(WallexModel):
    success: bool
    message: str = ""
    result: MarketRulesResult


# --- GET /v1/depth?symbol= (probe-verified) and GET /depth/all (Swagger) ------
# Swagger: resources.SingleMarketDepthResponse / AllMarketDepthResponse.
# Real capture: price and quantity are JSON numbers, sum is a string, and sum is
# the notional of that level (price * quantity), not a cumulative total.
# Asks are sorted ascending and bids descending, 50 levels each. /depth/all
# answered HTTP 503 (text/html) on every probe so far; /v1/depth works.


class DepthLevel(WallexModel):
    price: Decimal
    quantity: Decimal
    sum: Decimal  # price * quantity of this level


class MarketDepth(WallexModel):
    ask: list[DepthLevel] = Field(default_factory=list)
    bid: list[DepthLevel] = Field(default_factory=list)


class SingleMarketDepthResponse(WallexModel):
    success: bool
    message: str = ""
    result: MarketDepth


class AllMarketDepthResponse(WallexModel):
    success: bool
    message: str = ""
    result: dict[str, MarketDepth]


# --- GET /v1/trades?symbol= (probe-verified) ----------------------------------
# The last 50 public trades, newest first. isBuyOrder is assumed to be the taker
# side (UNVERIFIED semantics); it is a feature input only, never an order input.


class PublicTrade(WallexModel):
    symbol: str
    price: Decimal
    quantity: Decimal
    sum: Decimal
    is_buy_order: bool = Field(alias="isBuyOrder")
    timestamp: datetime


class TradesResult(WallexModel):
    latest_trades: list[PublicTrade] = Field(alias="latestTrades")


class TradesResponse(WallexModel):
    success: bool
    message: str = ""
    result: TradesResult
