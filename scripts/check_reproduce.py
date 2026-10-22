"""`make reproduce`: re-run the LLM ablation from the committed data and LLM cache (no model, no network)
and check that every metric matches the committed report reports/p4-llm_ablation/fee_table.csv."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main() -> int:
    import django

    django.setup()
    import pandas as pd
    from django.conf import settings

    from agent import experiments
    from backtest.config import load_study_config

    committed = PROJECT_ROOT / "reports" / "p4-llm_ablation" / "fee_table.csv"
    if not committed.exists():
        print("FAIL: no committed report to compare with")
        return 1
    config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "report"
        experiments.llm_ablation(
            config, settings.BASE_DIR / "data", out, Path(scratch), final=True, llm="replay", log=False
        )
        fresh = pd.read_csv(out / "fee_table.csv")
    expected = pd.read_csv(committed)
    keys = ["variant", "split", "fees"]
    merged = expected.merge(fresh, on=keys, suffixes=("_expected", "_fresh"))
    if len(merged) != len(expected):
        print(f"FAIL: rows differ ({len(fresh)} fresh vs {len(expected)} committed)")
        return 1
    worst = 0.0
    for column in [c for c in expected.columns if c not in keys]:
        diff = (merged[f"{column}_expected"] - merged[f"{column}_fresh"]).abs().max()
        worst = max(worst, float(diff))
    if worst > 1e-9:
        print(f"FAIL: metrics differ by up to {worst}")
        return 1
    print(f"ok: {len(merged)} fee-table rows reproduced exactly from data/ and data/llm_cache/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
