"""SMC research experiments (research/smc/experiment.py). Usage:
.venv/bin/python scripts/smc_experiment.py frequency          # setups per day per feature variant (no backtest)
.venv/bin/python scripts/smc_experiment.py selection          # A: 8 SMC configs + live (a), selection windows
.venv/bin/python scripts/smc_experiment.py final NAME         # A: NAME + live (a) on the holdout, once
.venv/bin/python scripts/smc_experiment.py recent NAME        # A: NAME + live (a), last 14 days
.venv/bin/python scripts/smc_experiment.py ml                 # B: SMC features in the h24 model
"""

import json
import os
import sys

sys.path.insert(0, ".")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import pandas as pd  # noqa: E402
from django.conf import settings  # noqa: E402

from backtest.config import load_study_config  # noqa: E402
from backtest.report import write_json  # noqa: E402
from execution.runner import live_limits, live_strategy_params, load_live_config  # noqa: E402
from research.smc import experiment as x  # noqa: E402

ROOT = settings.BASE_DIR
REPORTS = ROOT / "reports"


def main() -> None:
    command = sys.argv[1]
    config = x.measured(load_study_config(ROOT / "config/strategy.toml", ROOT))
    live = load_live_config()
    limits, live_params = live_limits(live), live_strategy_params(config, live)
    smc, tags, features, frames, data = x.build(config, ROOT)
    variants = x.smc_variants(smc, tags) + [(x.LIVE_REF, live_params)]
    if command == "frequency":
        start = x.first_prediction(features, ["BTCTMN", "ETHTMN"])
        out = {}
        for tag in tags:
            per = {}
            for symbol in ("BTCTMN", "ETHTMN"):
                column = pd.Series(features[symbol][f"setup_id_smc15{tag}"])
                column = column[column.index >= start].dropna()
                days = (features[symbol].index[-1] - start).total_seconds() / 86400
                per[symbol] = round(column.nunique() / days, 3)
            out[tag] = per
        print(json.dumps(out, indent=1))
    elif command == "selection":
        x.window_study(
            config,
            data,
            features,
            variants,
            limits,
            REPORTS / "smc-selection",
            REPORTS,
            False,
            "SMC A: smc_long grid vs live (a), selection windows",
        )
    elif command == "final":
        chosen = [v for v in variants if v[0] in (sys.argv[2], x.LIVE_REF)]
        x.window_study(
            config,
            data,
            features,
            chosen,
            limits,
            REPORTS / "smc-final",
            REPORTS,
            True,
            f"SMC A: {sys.argv[2]} vs live (a), holdout (--final)",
        )
    elif command == "recent":
        chosen = [v for v in variants if v[0] in (sys.argv[2], x.LIVE_REF)]
        result = x.recent(config, limits, data, features, chosen)
        write_json(REPORTS / "smc-recent" / "summary.json", result)
        from backtest.experiments import log_experiment

        log_experiment(
            REPORTS,
            f"SMC A: {sys.argv[2]} vs live (a), last 14 days",
            configs=2,
            holdout_evaluated=False,
            report="reports/smc-recent",
            notes="measured fees; live limits",
        )
        print(json.dumps(result, indent=1, default=str))
    elif command == "ml":
        from research.smc.ml import run_ml

        run_ml(config, ROOT, features, frames, data, live_params, limits, REPORTS)


if __name__ == "__main__":
    main()
