"""Experiment log: every study run is appended to reports/experiments.jsonl.

Judges (and we) need to know how many configurations were tried before a result was
picked, and whether the holdout was looked at. Studies call `log_experiment` themselves;
nothing is ever removed from the log.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

LOG_NAME = "experiments.jsonl"


def log_experiment(
    reports_dir: Path,
    name: str,
    configs: int,
    holdout_evaluated: bool,
    report: str | None = None,
    notes: str = "",
    config_hash: str | None = None,
) -> dict:
    entry = {
        "time": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "name": name,
        "configs": int(configs),
        "holdout_evaluated": bool(holdout_evaluated),
        "report": report,
        "config_hash": config_hash,
        "notes": notes,
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    with (reports_dir / LOG_NAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def read_log(reports_dir: Path) -> list[dict]:
    path = reports_dir / LOG_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def totals(entries: list[dict]) -> dict:
    return {
        "experiments": len(entries),
        "configs": sum(entry["configs"] for entry in entries),
        "holdout_evaluations": sum(entry["holdout_evaluated"] for entry in entries),
    }
