"""Builds the client and market policy from Django settings. Commands use these; tests do not."""

from django.conf import settings

from .client import WallexClient
from .ratelimit import RateLimiter
from .symbols import MarketPolicy, load_market_policy


def build_client() -> WallexClient:
    return WallexClient(
        base_url=settings.WALLEX_BASE_URL,
        api_key=settings.WALLEX_API_KEY,
        timeout_seconds=settings.WALLEX_TIMEOUT_SECONDS,
        max_retries=settings.WALLEX_MAX_RETRIES,
        rate_limiter=RateLimiter(settings.WALLEX_REQUESTS_PER_SECOND),
    )


def build_policy() -> MarketPolicy:
    return load_market_policy(settings.TRADING_CONFIG_FILE, settings.BLOCKED_MARKETS_FILE)
