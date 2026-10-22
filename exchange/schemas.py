"""Typed models for the Wallex responses this project uses.

Shapes come from docs/wallex_spot_swagger.json and were checked against real
captures in tests/fixtures/wallex/. Only the fields we use are declared. Unknown
keys are ignored, so new fields added by Wallex do not break a sync.

Numbers are Decimal: the client parses JSON numbers with parse_float=Decimal.
"""

from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _blank_to_none(value: Any) -> Any:
    """Real captures send "" or null for inactive markets (152 of 469 at capture time)."""
    return None if value in ("", None) else value


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


# --- GET /depth/all -----------------------------------------------------------
# Swagger: resources.AllMarketDepthResponse. Levels are JSON numbers in the spec.


class DepthLevel(WallexModel):
    price: Decimal
    quantity: Decimal
    sum: Decimal


class MarketDepth(WallexModel):
    ask: list[DepthLevel] = Field(default_factory=list)
    bid: list[DepthLevel] = Field(default_factory=list)


class AllMarketDepthResponse(WallexModel):
    success: bool
    message: str = ""
    result: dict[str, MarketDepth]
