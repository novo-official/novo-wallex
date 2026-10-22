import json

from django.conf import settings
from django.core.management.base import BaseCommand

from agent import experiments
from backtest.config import load_study_config
from backtest.experiment import print_tables


class Command(BaseCommand):
    help = "Phase 4, local LLM: benchmark | plan | precompute | ablation. No paid API is ever called."

    def add_arguments(self, parser):
        parser.add_argument("step", choices=["benchmark", "plan", "precompute", "ablation"])
        parser.add_argument(
            "--llm",
            default="replay",
            choices=["replay", "live-local"],
            help="replay: committed cache only (no model needed); live-local: the pinned Ollama model",
        )
        parser.add_argument("--final", action="store_true", help="include the holdout windows (logged; once)")
        parser.add_argument("--seconds-per-call", type=float, default=None, help="for the runtime estimate")

    def handle(self, *args, **options):
        config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
        data = settings.BASE_DIR / "data"
        reports = settings.BASE_DIR / "reports"
        step = options["step"]
        if step == "benchmark":
            summary = experiments.benchmark(config, data, reports / "p4-benchmark", reports)
            self.stdout.write(json.dumps({k: v for k, v in summary.items() if k != "schemas"}, indent=2))
        elif step == "plan":
            planned = experiments.plan(config, data, options["final"], options["seconds_per_call"])
            self.stdout.write(json.dumps({k: v for k, v in planned.items() if not k.startswith("_")}, indent=2))
        elif step == "precompute":
            progress = settings.BASE_DIR / "logs" / "p4_precompute_progress.jsonl"
            self.stdout.write(json.dumps(experiments.precompute(config, data, options["final"], progress)))
        else:
            result = experiments.llm_ablation(
                config, data, reports / "p4-llm_ablation", reports, final=options["final"], llm=options["llm"]
            )
            print_tables(result, self.stdout.write)
