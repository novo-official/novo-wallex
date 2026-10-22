from datetime import UTC, datetime

from django.conf import settings
from django.core.management.base import BaseCommand

from backtest.config import load_study_config
from backtest.experiment import print_tables
from backtest.pipeline import load_market, window_study


class Command(BaseCommand):
    help = "Rolling 3-day window study: grid on the selection windows, chosen params on the holdout (+ fee stress)."

    def add_arguments(self, parser):
        parser.add_argument("--config", default=str(settings.BASE_DIR / "config" / "strategy.toml"))
        parser.add_argument("--out", default=None, help="report directory (default: reports/windows-<time>)")
        parser.add_argument("--processes", type=int, default=None)
        parser.add_argument(
            "--final", action="store_true", help="also run the holdout windows (logged; once per model)"
        )

    def handle(self, *args, **options):
        from pathlib import Path

        config = load_study_config(Path(options["config"]), settings.BASE_DIR)
        data, manifest = load_market(config, settings.BASE_DIR / "data")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = Path(options["out"]) if options["out"] else settings.BASE_DIR / "reports" / f"windows-{stamp}"
        result = window_study(
            config, data, manifest, out, settings.BASE_DIR / "reports", options["processes"], options["final"]
        )
        print_tables(result, self.stdout.write)
        self.stdout.write(f"report: {out}")
