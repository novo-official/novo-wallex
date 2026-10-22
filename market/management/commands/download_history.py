from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from exchange.factory import build_client, build_policy
from exchange.symbols import MarketPolicyError
from market import services
from market.config import load_market_config
from market.models import Market


class Command(BaseCommand):
    help = "Download completed OHLCV bars for allowlisted markets into the Candle table (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--symbol",
            action="append",
            help="API symbol or BASE/QUOTE; repeat for several. Default: all allowed markets.",
        )
        parser.add_argument("--resolution", default="60", help="UDF resolution in minutes (default: %(default)s)")
        parser.add_argument("--days", type=float, default=30.0, help="window in days ending now (default: %(default)s)")
        parser.add_argument("--start", help="ISO 8601 start, UTC. Overrides --days.")
        parser.add_argument("--end", help="ISO 8601 end, UTC. Default: now.")

    def handle(self, *args, **options):
        policy = build_policy()
        config = load_market_config(settings.TRADING_CONFIG_FILE)
        end = _parse_time(options["end"]) if options["end"] else datetime.now(UTC)
        start = _parse_time(options["start"]) if options["start"] else end - timedelta(days=options["days"])
        if start >= end:
            raise CommandError("start must be before end")

        symbols = options["symbol"] or list(Market.objects.filter(is_allowed=True).values_list("symbol", flat=True))
        if not symbols:
            raise CommandError("no allowed markets in the DB; run sync_markets first")

        with build_client() as client:
            for symbol in symbols:
                try:
                    summary = services.download_candles(
                        client, policy, symbol, options["resolution"], start, end, config.chunk_bars
                    )
                except (MarketPolicyError, Market.DoesNotExist) as exc:
                    raise CommandError(f"{symbol}: {exc}") from exc
                self.stdout.write(
                    f"{symbol} resolution={options['resolution']} requests={summary.requests} "
                    f"received={summary.bars_received} written={summary.bars_written}"
                )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise CommandError(f"{value}: timezone required, e.g. 2026-10-01T00:00:00+00:00")
    return parsed.astimezone(UTC)
