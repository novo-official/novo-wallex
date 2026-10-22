from django.conf import settings
from django.core.management.base import BaseCommand

from exchange.factory import build_policy
from market import services
from market.config import load_market_config


class Command(BaseCommand):
    help = "Show which allowlisted markets are eligible now (policy, quote asset, 24h volume, spread)."

    def handle(self, *args, **options):
        policy = build_policy()
        config = load_market_config(settings.TRADING_CONFIG_FILE)
        verdicts, missing = services.build_universe(policy, config.universe)
        for verdict in verdicts:
            status = "ELIGIBLE" if verdict.eligible else "REJECTED"
            detail = "; ".join(verdict.reasons) if verdict.reasons else "-"
            self.stdout.write(f"{verdict.symbol:<12} {status:<9} {detail}")
        eligible = sum(verdict.eligible for verdict in verdicts)
        self.stdout.write(f"eligible={eligible} of {len(verdicts)} allowlisted")
        if missing:
            self.stdout.write(f"not in DB (run sync_markets): {', '.join(missing)}")
