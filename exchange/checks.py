"""Startup system check: the market policy must load before the project runs.

Django runs registered checks on `manage.py check`, `runserver`, `migrate` and
engine commands, so a broken or missing blocklist stops the process early.
"""

import tomllib

from django.conf import settings
from django.core import checks

from .symbols import MarketPolicyError, load_market_policy


@checks.register("exchange")
def check_market_policy(app_configs, **kwargs):
    try:
        policy = load_market_policy(settings.TRADING_CONFIG_FILE, settings.BLOCKED_MARKETS_FILE)
    except (OSError, MarketPolicyError, tomllib.TOMLDecodeError) as exc:
        return [checks.Error(f"market policy is invalid: {exc}", id="exchange.E001")]
    if not policy.allowlist:
        return [
            checks.Warning(
                "allowlist is empty: no market is tradable",
                hint="Add markets to markets.allowlist in config/trading.toml.",
                id="exchange.W001",
            )
        ]
    return []
