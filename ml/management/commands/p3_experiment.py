from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backtest.config import load_study_config
from ml import p3_experiments


class Command(BaseCommand):
    help = "Run one Phase 3 experiment (logged). Holdout only with --final."

    def add_arguments(self, parser):
        parser.add_argument("name", help="experiment: ml_targets, ml_targets_solxrp, ...")
        parser.add_argument("--final", action="store_true")
        parser.add_argument("--out", default=None)

    def handle(self, *args, **options):
        config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
        name = options["name"]
        out = Path(options["out"]).resolve() if options["out"] else settings.BASE_DIR / "reports" / f"p3-{name}"
        reports = settings.BASE_DIR / "reports"
        data = settings.BASE_DIR / "data"
        if name == "ml_targets":
            results = p3_experiments.ml_targets(config, data, out, reports)
        elif name == "ml_targets_solxrp":
            results = p3_experiments.ml_targets(config, data, out, reports, symbols=["SOLTMN", "XRPTMN"])
        elif name == "explain_h48":
            result = p3_experiments.explain_h48(config, data, out, reports)
            self.stdout.write(f"model {result['model_id']}")
            self.stdout.write(result["shap"].head(15).round(6).to_string())
            return
        elif hasattr(p3_experiments, name):
            result = getattr(p3_experiments, name)(config, data, out, reports, final=options["final"])
            p3_experiments.print_result(result, self.stdout.write)
            return
        else:
            raise CommandError(f"unknown experiment {name!r}")
        self.stdout.write(p3_experiments.summarize_targets(results).round(5).to_string())
        self.stdout.write(f"report: {out}")
