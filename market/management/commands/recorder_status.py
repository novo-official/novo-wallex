from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand

from market.config import load_market_config
from market.recorder_status import summarize


class Command(BaseCommand):
    help = "Summarize recorded order book data: coverage, request errors, latency and spreads per symbol."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=float, default=1.0, help="window ending now (default: %(default)s)")

    def handle(self, *args, **options):
        config = load_market_config(settings.TRADING_CONFIG_FILE).recorder
        root = config.out_dir if config.out_dir.is_absolute() else settings.BASE_DIR / config.out_dir
        end = datetime.now(UTC)
        status = summarize(root, end - timedelta(hours=options["hours"]), end, config.depth_interval_seconds)
        error_rate = status.errors / status.requests if status.requests else 0.0
        self.stdout.write(f"window {status.start:%Y-%m-%d %H:%M} .. {status.end:%H:%M} UTC")
        self.stdout.write(
            f"requests={status.requests} errors={status.errors} ({error_rate:.2%}) "
            f"median_latency_ms={status.median_latency_ms} median_clock_offset_ms={status.median_clock_offset_ms}"
        )
        self.stdout.write(f"trades={status.trades} ticker_snapshots={status.ticker_snapshots} notes={status.notes}")
        self.stdout.write(
            f"{'symbol':<10} {'snapshots':>9} {'coverage':>8} {'spread_med_bps':>14} {'spread_p90_bps':>14}"
        )
        for row in status.symbols:
            median = f"{row.median_spread_bps:.1f}" if row.median_spread_bps is not None else "-"
            p90 = f"{row.p90_spread_bps:.1f}" if row.p90_spread_bps is not None else "-"
            self.stdout.write(f"{row.symbol:<10} {row.snapshots:>9} {row.coverage:>8.1%} {median:>14} {p90:>14}")
