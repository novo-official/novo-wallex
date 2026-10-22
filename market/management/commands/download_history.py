from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from exchange.client import WallexClient
from exchange.factory import build_policy
from exchange.ratelimit import RateLimiter
from exchange.symbols import MarketPolicyError
from market import services
from market.config import load_market_config
from market.models import Candle, Market
from market.recorder import ordered_symbols


class Command(BaseCommand):
    help = "Download completed OHLCV bars for data-universe markets into the Candle table (idempotent, resumable)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--symbol",
            action="append",
            help="API symbol or BASE/QUOTE; repeat for several. Default: the whole data universe.",
        )
        parser.add_argument("--resolution", default="1", help="UDF resolution in minutes (default: %(default)s)")
        parser.add_argument("--days", type=float, default=30.0, help="window in days ending now (default: %(default)s)")
        parser.add_argument("--start", help="ISO 8601 start, UTC. Overrides --days.")
        parser.add_argument("--end", help="ISO 8601 end, UTC. Default: now.")
        parser.add_argument("--resume", action="store_true", help="start each symbol after its last stored bar")

    def handle(self, *args, **options):
        policy = build_policy()
        config = load_market_config(settings.TRADING_CONFIG_FILE)
        end = _parse_time(options["end"]) if options["end"] else datetime.now(UTC)
        start = _parse_time(options["start"]) if options["start"] else end - timedelta(days=options["days"])
        if start >= end:
            raise CommandError("start must be before end")

        symbols = options["symbol"] or ordered_symbols(policy)
        client = WallexClient(
            base_url=settings.WALLEX_BASE_URL,
            api_key="",  # public endpoint; no key needed
            timeout_seconds=settings.WALLEX_TIMEOUT_SECONDS,
            max_retries=settings.WALLEX_MAX_RETRIES,
            rate_limiter=RateLimiter(config.history_requests_per_second),
        )
        with client:
            for symbol in symbols:
                symbol_start = start
                if options["resume"]:
                    last = (
                        Candle.objects.filter(market__symbol=symbol, resolution=options["resolution"])
                        .order_by("-open_time")
                        .values_list("open_time", flat=True)
                        .first()
                    )
                    if last is not None:
                        symbol_start = max(start, last + timedelta(minutes=1))
                try:
                    summary = services.download_candles(
                        client, policy, symbol, options["resolution"], symbol_start, end, config.chunk_bars
                    )
                except (MarketPolicyError, Market.DoesNotExist) as exc:
                    raise CommandError(f"{symbol}: {exc}") from exc
                self.stdout.write(
                    f"{symbol} resolution={options['resolution']} requests={summary.requests} "
                    f"received={summary.bars_received} written={summary.bars_written} "
                    f"suspicious_windows={summary.suspicious_windows}"
                )
                self.stdout.flush()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise CommandError(f"{value}: timezone required, e.g. 2026-10-01T00:00:00+00:00")
    return parsed.astimezone(UTC)
