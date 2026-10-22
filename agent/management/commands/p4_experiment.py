import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from agent import experiments, replay
from backtest.config import load_study_config
from backtest.experiment import print_tables


class Command(BaseCommand):
    help = "Phase 4 LLM steps: plan (free), submit (spends money: needs --confirm-spend), collect, ablation."

    def add_arguments(self, parser):
        parser.add_argument("step", choices=["plan", "submit", "collect", "ablation"])
        parser.add_argument("--batch-id", default=None)
        parser.add_argument("--confirm-spend", action="store_true", help="required for submit")
        parser.add_argument("--final", action="store_true", help="include the holdout windows (logged; once)")
        parser.add_argument("--agent-mode", default="live", choices=["live", "replay"])

    def handle(self, *args, **options):
        config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
        data = settings.BASE_DIR / "data"
        step = options["step"]
        record = settings.BASE_DIR / "reports" / "llm_batches.jsonl"
        if step in ("plan", "submit", "collect"):
            plan = experiments.llm_plan(config, data, final=options["final"])
            summary = {k: v for k, v in plan.items() if k != "requests"}
            self.stdout.write(json.dumps(summary, indent=2))
            if step == "submit":
                if not plan["requests"]:
                    self.stdout.write("nothing to submit: every officer request is cached")
                    return
                if not options["confirm_spend"]:
                    raise CommandError(f"submitting costs about ${plan['to_buy']['usd_est']}; add --confirm-spend")
                batch_id = replay.submit_batch(plan["requests"], record)
                self.stdout.write(f"submitted batch {batch_id}; run: p4_experiment collect --batch-id {batch_id}")
            elif step == "collect":
                if not options["batch_id"]:
                    raise CommandError("--batch-id is required")
                all_requests = replay.officer_requests(*_windows(config, data, options["final"]))
                self.stdout.write(json.dumps(replay.collect_batch(options["batch_id"], all_requests)))
            return
        out = settings.BASE_DIR / "reports" / "p4-llm_ablation"
        result = experiments.llm_ablation(
            config, data, out, settings.BASE_DIR / "reports", final=options["final"], agent_mode=options["agent_mode"]
        )
        print_tables(result, self.stdout.write)


def _windows(config, data_dir: Path, final: bool):
    from backtest.experiment import select_windows

    config = experiments._config(config)
    market, first = experiments._market(config, data_dir)
    selection, holdout = select_windows(market, config, first)
    return market, selection + (holdout if final else []), config.windows.window_days
