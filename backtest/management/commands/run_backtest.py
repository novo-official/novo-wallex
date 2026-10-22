import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand

from backtest.config import load_study_config
from backtest.pipeline import load_market, run_single


class Command(BaseCommand):
    help = "One backtest of the canonical [strategy] params over a period; writes reports/<run_id>/."

    def add_arguments(self, parser):
        parser.add_argument("--config", default=str(settings.BASE_DIR / "config" / "strategy.toml"))
        parser.add_argument("--start", help="ISO start (default: data start + warmup)")
        parser.add_argument("--end", help="ISO end (default: data end)")
        parser.add_argument("--params", help="JSON params instead of [strategy]")
        parser.add_argument("--fee-stress", action="store_true")
        parser.add_argument("--out", default=None)

    def handle(self, *args, **options):
        config = load_study_config(Path(options["config"]), settings.BASE_DIR)
        data, manifest = load_market(config, settings.BASE_DIR / "data")
        start = (
            pd.Timestamp(options["start"])
            if options["start"]
            else data.clock[0] + pd.Timedelta(days=config.windows.warmup_days)
        )
        end = pd.Timestamp(options["end"]) if options["end"] else data.clock[-1]
        params = json.loads(options["params"]) if options["params"] else config.strategy
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = Path(options["out"]) if options["out"] else settings.BASE_DIR / "reports" / f"backtest-{stamp}"
        metrics = run_single(config, data, manifest, params, start, end, out, options["fee_stress"])
        self.stdout.write(json.dumps(metrics, indent=2, default=str))
        self.stdout.write(f"report: {out}")
