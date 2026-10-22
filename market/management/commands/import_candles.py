from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from market.candles_io import read_candles, verify_manifest
from market.models import Candle, Market

BATCH = 5_000


class Command(BaseCommand):
    help = "Verify data/MANIFEST.json and load data/candles/<res>m/*.parquet into the Candle table (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--resolution", default="1")
        parser.add_argument("--data-dir", default=str(settings.BASE_DIR / "data"))

    def handle(self, *args, **options):
        data_dir = Path(options["data_dir"])
        prefix = f"candles/{options['resolution']}m/"
        checked = verify_manifest(data_dir, only_prefix=prefix)  # raises on any sha256 mismatch
        self.stdout.write(f"manifest ok: {checked} candle files verified")
        for path in sorted((data_dir / prefix).glob("*.parquet")):
            symbol = path.stem
            market = Market.objects.filter(symbol=symbol).first()
            if market is None:
                raise CommandError(f"{symbol}: market not in the DB; run sync_markets first")
            rows = read_candles(path).to_pylist()
            for start in range(0, len(rows), BATCH):
                Candle.objects.bulk_create(
                    [
                        Candle(
                            market=market,
                            resolution=options["resolution"],
                            open_time=row["open_time"],
                            open_price=row["open"],
                            high_price=row["high"],
                            low_price=row["low"],
                            close_price=row["close"],
                            volume=row["volume"],
                        )
                        for row in rows[start : start + BATCH]
                    ],
                    update_conflicts=True,
                    unique_fields=["market", "resolution", "open_time"],
                    update_fields=["open_price", "high_price", "low_price", "close_price", "volume"],
                )
            self.stdout.write(f"{symbol}: {len(rows)} bars imported")
