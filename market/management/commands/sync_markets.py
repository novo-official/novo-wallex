from django.core.management.base import BaseCommand

from exchange.factory import build_client, build_policy
from market import services


class Command(BaseCommand):
    help = "Refresh spot markets (precision, volumes, blocked and allowed flags) from Wallex."

    def handle(self, *args, **options):
        policy = build_policy()
        with build_client() as client:
            summary = services.sync_markets(client, policy)
        self.stdout.write(
            f"spot markets stored={summary.spot_markets} allowed={summary.allowed} "
            f"blocked={summary.blocked} skipped_non_spot={summary.skipped_non_spot} "
            f"without_rules={summary.without_rules}"
        )
