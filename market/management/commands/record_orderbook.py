import logging
import signal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from exchange.client import WallexClient
from exchange.factory import build_policy
from exchange.ratelimit import RateLimiter
from market.config import load_market_config
from market.recorder import OrderbookRecorder


class Command(BaseCommand):
    help = "Record order book depth, public trades and tickers to hourly Parquet files (GET only, runs until stopped)."

    def add_arguments(self, parser):
        parser.add_argument("--out-dir", help="output root (default: recorder.out_dir in config/trading.toml)")
        parser.add_argument("--ticks", type=int, default=None, help="stop after this many ticks (default: run forever)")

    def handle(self, *args, **options):
        logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request is too much for days
        policy = build_policy()
        config = load_market_config(settings.TRADING_CONFIG_FILE).recorder
        out_dir = Path(options["out_dir"]) if options["out_dir"] else config.out_dir
        out_dir = out_dir if out_dir.is_absolute() else settings.BASE_DIR / out_dir

        stop = {"requested": False}

        def request_stop(signum, frame):
            stop["requested"] = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        client = WallexClient(
            base_url=settings.WALLEX_BASE_URL,
            api_key="",  # public endpoints only: nothing account-specific can reach the files
            timeout_seconds=settings.WALLEX_TIMEOUT_SECONDS,
            max_retries=1,  # a slow retry chain would delay the whole tick; the next tick retries anyway
            rate_limiter=RateLimiter(config.max_requests_per_second),
        )
        with client:
            recorder = OrderbookRecorder(client, policy, config, out_dir)
            limit = options["ticks"]
            self.stdout.write(f"recording {len(recorder.symbols)} symbols to {out_dir}: {', '.join(recorder.symbols)}")
            recorder.run(lambda: stop["requested"] or (limit is not None and recorder.ticks >= limit))
        self.stdout.write(f"stopped after {recorder.ticks} ticks")
