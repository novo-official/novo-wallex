"""Control for the LLM ablation: the trend rules with every entry scaled to the Trade Agent's average exposure.

The Trade Agent variant held 0.158 average exposure vs 0.209 for the rules (selection, base fees), i.e. ~0.76x.
If the rules at a fixed 0.76x size show the same drawdown reduction, the LLM's risk benefit is a size cut, not
judgment. Same 13 selection windows, no LLM. Usage: .venv/bin/python scripts/ablation_size_control.py
"""

import os
import sys
from dataclasses import replace

sys.path.insert(0, ".")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from agent.experiments import _manifest, _windows  # noqa: E402
from backtest.config import load_study_config  # noqa: E402
from backtest.experiment import run_experiment  # noqa: E402
from strategy.factory import build_strategy  # noqa: E402

SCALE = 0.158 / 0.209


class Scaled:
    def __init__(self, inner, scale: float) -> None:
        self.inner, self.scale = inner, scale
        self.name = inner.name

    def on_cycle(self, ctx, portfolio):
        return [
            replace(i, size_fraction=i.size_fraction * self.scale) if i.kind == "ENTER" else i
            for i in self.inner.on_cycle(ctx, portfolio)
        ]

    def __getattr__(self, item):
        return getattr(self.inner, item)


def factory(params, symbols):
    params = dict(params)
    scale = params.pop("size_scale_control", None)
    strategy = build_strategy(params, symbols)
    return Scaled(strategy, scale) if scale else strategy


def main() -> None:
    config = load_study_config(settings.BASE_DIR / "config/strategy.toml", settings.BASE_DIR)
    config, data, selection, holdout, _ = _windows(config, settings.BASE_DIR / "data", False)
    base = dict(config.strategy)
    run_experiment(
        "P4 ablation control: rules at the trade agent's average exposure (no LLM)",
        config,
        data,
        _manifest(settings.BASE_DIR / "data"),
        [("rules", base), (f"rules x{SCALE:.2f} size", {**base, "size_scale_control": SCALE})],
        settings.BASE_DIR / "reports" / "p4-ablation_size_control",
        settings.BASE_DIR / "reports",
        references=("rules",),
        windows_override=(selection, holdout),
        factory=factory,
        processes=1,
        notes="control for the LLM ablation; same 13 selection windows. The two previous entries of this name "
        "did not pass the factory (size scaling not applied, identical to rules): invalid",
    )


if __name__ == "__main__":
    main()
