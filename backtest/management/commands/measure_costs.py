import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from backtest.config import load_study_config
from backtest.costs import measure_slippage


class Command(BaseCommand):
    help = "Measure taker slippage per symbol on the recorded order book and freeze it in config/slippage.json."

    def add_arguments(self, parser):
        parser.add_argument("--notional", type=float, default=1_000_000, help="order size in TMN (default: 1M)")
        parser.add_argument("--floor", default="0.0002", help="minimum slippage fraction (default: 2 bps)")

    def handle(self, *args, **options):
        config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
        slippage, evidence = measure_slippage(
            settings.BASE_DIR / "data" / "orderbook", config.tradable, options["notional"], Decimal(options["floor"])
        )
        payload = {
            "measured_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "method": "median over recorded /v1/depth snapshots of (half spread + book impact) for a "
            f"{options['notional']:.0f} TMN taker order, buy and sell averaged; floor {options['floor']}",
            "slippage": {symbol: float(value) for symbol, value in sorted(slippage.items())},
            "evidence": evidence,
        }
        path = Path(settings.BASE_DIR) / config.raw["backtest"]["slippage_file"]
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        for symbol, value in sorted(slippage.items()):
            self.stdout.write(f"{symbol:<8} {float(value) * 1e4:6.1f} bps  ({evidence[symbol]['snapshots']} snapshots)")
        self.stdout.write(f"written to {path}")
