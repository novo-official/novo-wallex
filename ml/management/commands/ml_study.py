import json
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from backtest.config import load_study_config
from ml.study import run_ml_study


class Command(BaseCommand):
    help = "Walk-forward ML entry filter and the rules vs rules+ML ablation on 3-day windows; saves the final model."

    def add_arguments(self, parser):
        parser.add_argument("--config", default=str(settings.BASE_DIR / "config" / "strategy.toml"))
        parser.add_argument("--out", default=None, help="report directory (default: reports/ml-<time>)")
        parser.add_argument("--models-dir", default=str(settings.BASE_DIR / "models"))
        parser.add_argument("--processes", type=int, default=None)

    def handle(self, *args, **options):
        config = load_study_config(Path(options["config"]), settings.BASE_DIR)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = Path(options["out"]) if options["out"] else settings.BASE_DIR / "reports" / f"ml-{stamp}"
        summary = run_ml_study(
            config, settings.BASE_DIR / "data", Path(options["models_dir"]), out, options["processes"]
        )
        self.stdout.write(json.dumps(summary, indent=2, default=str))
        self.stdout.write(f"report: {out}")
