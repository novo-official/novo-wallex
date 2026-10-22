"""Database-backed market operations: sync markets, sync orderbook snapshots, download candles, build universe.

Every function takes its client and policy as arguments, so tests can inject a
mock transport and the management commands stay thin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db import transaction

from exchange.client import WallexClient, WallexError
from exchange.schemas import AllMarketDepthResponse, MarketDepth, MarketsResponse, UdfHistoryResponse
from exchange.symbols import MarketPolicy, MarketPolicyError, to_api_symbol

from .history import Bar, floor_to_step, parse_bars, resolution_seconds, split_windows
from .models import Candle, Market, OrderbookSnapshot
from .universe import Candidate, UniverseConfig, Verdict, evaluate, spread_fraction

logger = logging.getLogger(__name__)

CANDLE_VALUE_FIELDS = ["open_price", "high_price", "low_price", "close_price", "volume"]


@dataclass(frozen=True)
class SyncSummary:
    spot_markets: int
    allowed: int
    blocked: int
    skipped_non_spot: int


@dataclass(frozen=True)
class DownloadSummary:
    requests: int
    bars_received: int
    bars_written: int


def sync_markets(client: WallexClient, policy: MarketPolicy) -> SyncSummary:
    """Upsert spot markets with precision, last price, volumes and the blocked/allowed flags."""
    response = MarketsResponse.model_validate(client.get_markets())
    if not response.success:
        raise WallexError(f"markets request was not successful: {response.message}")

    spot_markets = skipped = allowed = blocked = 0
    with transaction.atomic():
        for info in response.result.markets:
            if not info.is_spot:
                skipped += 1
                continue
            symbol = to_api_symbol(info.symbol)
            is_blocked = symbol in policy.blocked
            is_allowed = policy.is_tradable(symbol)
            Market.objects.update_or_create(
                symbol=symbol,
                defaults={
                    "base_asset": info.base_asset,
                    "quote_asset": info.quote_asset,
                    "amount_precision": info.amount_precision,
                    "price_precision": info.price_precision,
                    "last_price": info.price,
                    "volume_24h": info.volume_24h,
                    "quote_volume_24h": info.quote_volume_24h,
                    "is_blocked": is_blocked,
                    "is_allowed": is_allowed,
                },
            )
            spot_markets += 1
            allowed += is_allowed
            blocked += is_blocked
    return SyncSummary(spot_markets=spot_markets, allowed=allowed, blocked=blocked, skipped_non_spot=skipped)


def _top_of_book(depth: MarketDepth) -> tuple[Decimal, Decimal] | None:
    """Best bid (highest) and best ask (lowest). None when either side is empty."""
    if not depth.bid or not depth.ask:
        return None
    return max(level.price for level in depth.bid), min(level.price for level in depth.ask)


def sync_orderbook(client: WallexClient, policy: MarketPolicy, captured_at: datetime) -> int:
    """Store one top-of-book snapshot per allowlisted market. Returns the number stored."""
    response = AllMarketDepthResponse.model_validate(client.get_depth_all())
    if not response.success:
        raise WallexError(f"depth request was not successful: {response.message}")

    stored = 0
    for symbol, depth in response.result.items():
        if not policy.is_tradable(symbol):
            continue
        market = Market.objects.filter(symbol=symbol).first()
        top = _top_of_book(depth)
        if market is None or top is None:
            logger.info("no snapshot for %s (market row %s, top of book %s)", symbol, market is not None, top)
            continue
        best_bid, best_ask = top
        try:
            spread = spread_fraction(best_bid, best_ask)
        except ValueError as exc:
            logger.warning("skipping %s: %s", symbol, exc)
            continue
        OrderbookSnapshot.objects.create(
            market=market,
            captured_at=captured_at,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_fraction=spread,
        )
        stored += 1
    return stored


def upsert_candles(market: Market, resolution: str, bars: list[Bar]) -> int:
    """Insert new bars and update existing ones. Re-running with the same data changes nothing."""
    unique_bars = {bar.open_time: bar for bar in bars}  # Postgres rejects duplicate keys in one upsert
    if not unique_bars:
        return 0
    Candle.objects.bulk_create(
        [
            Candle(
                market=market,
                resolution=resolution,
                open_time=bar.open_time,
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                close_price=bar.close_price,
                volume=bar.volume,
            )
            for bar in unique_bars.values()
        ],
        update_conflicts=True,
        unique_fields=["market", "resolution", "open_time"],
        update_fields=CANDLE_VALUE_FIELDS,
    )
    return len(unique_bars)


def download_candles(
    client: WallexClient,
    policy: MarketPolicy,
    symbol: str,
    resolution: str,
    start: datetime,
    end: datetime,
    chunk_bars: int,
) -> DownloadSummary:
    """Download completed bars in [start, end) for one allowlisted market.

    The policy check runs before any request. The bar that is still open at
    `end` is excluded, so only complete bars are stored.
    """
    api_symbol = to_api_symbol(symbol)
    policy.check(api_symbol)  # raises for blocked or non-allowlisted markets, before any request
    market = Market.objects.get(symbol=api_symbol)

    step = resolution_seconds(resolution)
    window_start = floor_to_step(int(start.timestamp()), step)
    window_end = floor_to_step(int(end.timestamp()), step)

    requests = bars_received = bars_written = 0
    for from_ts, to_ts in split_windows(window_start, window_end, step, chunk_bars):
        raw = client.get_udf_history(api_symbol, resolution, from_ts, to_ts)
        requests += 1
        bars = parse_bars(UdfHistoryResponse.model_validate(raw))
        in_window = [bar for bar in bars if from_ts <= int(bar.open_time.timestamp()) <= to_ts]
        bars_received += len(in_window)
        bars_written += upsert_candles(market, resolution, in_window)
    return DownloadSummary(requests=requests, bars_received=bars_received, bars_written=bars_written)


def build_universe(policy: MarketPolicy, config: UniverseConfig) -> tuple[list[Verdict], list[str]]:
    """Evaluate every allowlisted market. Returns (verdicts, allowlisted symbols missing from the DB).

    Candidates start from the allowlist, not the whole market list. Default-deny applies:
    a market is never eligible just because it is in the DB.
    """
    candidates: list[Candidate] = []
    missing: list[str] = []
    for symbol in sorted(policy.allowlist):
        market = Market.objects.filter(symbol=symbol).first()
        if market is None:
            missing.append(symbol)
            continue
        snapshot = OrderbookSnapshot.objects.filter(market=market).first()
        try:
            policy.check(symbol)
            policy_error = None
        except MarketPolicyError as exc:
            policy_error = str(exc)
        candidates.append(
            Candidate(
                symbol=symbol,
                quote_asset=market.quote_asset,
                policy_error=policy_error,
                quote_volume_24h=market.quote_volume_24h,
                spread_fraction=snapshot.spread_fraction if snapshot else None,
            )
        )
    return evaluate(candidates, config), missing
