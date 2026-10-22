"""Order book features from one depth snapshot. Pure; not used for training yet.

The recorder history is too short to train on (it started 2026-10-22). These features are
computed for every recorded snapshot and, from Phase 5, logged with every decision, so a later
phase can test them once weeks of history exist.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BookFeatures:
    spread_bps: float
    imbalance_top5: float  # (bid qty - ask qty) / (bid qty + ask qty) over the best 5 levels, in quote value
    bid_depth_50bps: float  # quote value resting within 0.5% of mid
    ask_depth_50bps: float
    microprice_offset_bps: float  # size-weighted mid minus mid


def book_features(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> BookFeatures | None:
    """`bids` best first (descending), `asks` best first (ascending), as (price, quantity)."""
    if not bids or not asks or bids[0][0] <= 0 or asks[0][0] < bids[0][0]:
        return None
    best_bid, bid_size = bids[0]
    best_ask, ask_size = asks[0]
    mid = (best_bid + best_ask) / 2
    bid_value = sum(price * quantity for price, quantity in bids[:5])
    ask_value = sum(price * quantity for price, quantity in asks[:5])
    total = bid_value + ask_value
    microprice = (best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size) if bid_size + ask_size else mid
    return BookFeatures(
        spread_bps=(best_ask - best_bid) / mid * 1e4,
        imbalance_top5=(bid_value - ask_value) / total if total else 0.0,
        bid_depth_50bps=sum(p * q for p, q in bids if p >= mid * 0.995),
        ask_depth_50bps=sum(p * q for p, q in asks if p <= mid * 1.005),
        microprice_offset_bps=(microprice - mid) / mid * 1e4,
    )
