from datetime import UTC, datetime

from django.core.management.base import BaseCommand

from exchange.factory import build_client, build_policy
from market import services


class Command(BaseCommand):
    help = "Store one best-bid/best-ask snapshot per allowlisted market (used by the spread filter)."

    def handle(self, *args, **options):
        policy = build_policy()
        with build_client() as client:
            stored = services.sync_orderbook(client, policy, captured_at=datetime.now(UTC))
        self.stdout.write(f"orderbook snapshots stored={stored}")
