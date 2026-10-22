import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from exchange.factory import build_policy
from market.candles_io import candle_path, write_candles, write_manifest
from market.models import Candle, Market
from market.recorder import ordered_symbols


class Command(BaseCommand):
    help = "Export candles from the DB to data/candles/<res>m/<SYMBOL>.parquet and rewrite data/MANIFEST.json."

    def add_arguments(self, parser):
        parser.add_argument("--resolution", default="1")
        parser.add_argument("--data-dir", default=str(settings.BASE_DIR / "data"))

    def handle(self, *args, **options):
        data_dir = Path(options["data_dir"])
        symbols = ordered_symbols(build_policy())
        self._write_rules(data_dir, symbols)
        for symbol in symbols:
            rows = [
                {"open_time": t, "open": o, "high": h, "low": low, "close": c, "volume": v}
                for t, o, h, low, c, v in Candle.objects.filter(market__symbol=symbol, resolution=options["resolution"])
                .order_by("open_time")
                .values_list("open_time", "open_price", "high_price", "low_price", "close_price", "volume")
                .iterator(chunk_size=50_000)
            ]
            if not rows:
                self.stdout.write(f"{symbol}: no candles, skipped")
                continue
            path = candle_path(data_dir, options["resolution"], symbol)
            count = write_candles(path, rows)
            self.stdout.write(f"{symbol}: {count} bars -> {path.relative_to(data_dir)} ({path.stat().st_size} bytes)")
        manifest = write_manifest(data_dir)
        self.stdout.write(f"manifest: {len(manifest['files'])} files")

    def _write_rules(self, data_dir: Path, symbols: list[str]) -> None:
        """Snapshot of the exchange rules the backtester needs (precision, minimum notional)."""
        markets = {}
        for market in Market.objects.filter(symbol__in=symbols).order_by("symbol"):
            if market.min_order_value is None:
                continue  # rules not synced: the backtester will refuse to trade this market
            markets[market.symbol] = {
                "amount_decimals": market.amount_precision,
                "price_decimals": market.price_precision,
                "min_notional": format(market.min_order_value.normalize(), "f"),
                "min_qty": format(market.min_qty.normalize(), "f") if market.min_qty is not None else None,
                "market_orders_enabled": market.market_orders_enabled,
            }
        data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"source": "GET /v1/markets + /hector/web/v1/markets via sync_markets", "markets": markets}
        (data_dir / "market_rules.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
