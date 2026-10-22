"""Universe selection: which allowlisted markets are eligible to trade right now.

`evaluate` is pure: it takes already-loaded facts and returns a verdict with
reasons for each market, so every rejection can be explained in the decision log.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class UniverseConfig:
    quote_asset: str
    min_quote_volume_24h: Decimal
    max_spread_fraction: Decimal


@dataclass(frozen=True)
class Candidate:
    symbol: str
    quote_asset: str
    policy_error: str | None  # None when the market policy allows it, else the reason
    quote_volume_24h: Decimal | None
    spread_fraction: Decimal | None  # None when there is no orderbook snapshot


@dataclass(frozen=True)
class Verdict:
    symbol: str
    eligible: bool
    reasons: tuple[str, ...]  # empty when eligible


def spread_fraction(best_bid: Decimal, best_ask: Decimal) -> Decimal:
    """(ask - bid) / mid, where mid = (ask + bid) / 2."""
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        raise ValueError(f"invalid top of book: bid={best_bid} ask={best_ask}")
    mid = (best_ask + best_bid) / 2
    return (best_ask - best_bid) / mid


def evaluate(candidates: list[Candidate], config: UniverseConfig) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for candidate in candidates:
        reasons: list[str] = []
        if candidate.policy_error is not None:
            reasons.append(candidate.policy_error)
        if candidate.quote_asset != config.quote_asset:
            reasons.append(f"quote asset {candidate.quote_asset} is not {config.quote_asset}")
        if candidate.quote_volume_24h is None:
            reasons.append("no 24h volume data")
        elif candidate.quote_volume_24h < config.min_quote_volume_24h:
            reasons.append(f"24h quote volume {candidate.quote_volume_24h} below {config.min_quote_volume_24h}")
        if candidate.spread_fraction is None:
            reasons.append("no orderbook snapshot")
        elif candidate.spread_fraction > config.max_spread_fraction:
            reasons.append(f"spread {candidate.spread_fraction} above {config.max_spread_fraction}")
        verdicts.append(Verdict(symbol=candidate.symbol, eligible=not reasons, reasons=tuple(reasons)))
    return verdicts
