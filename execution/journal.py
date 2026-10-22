"""DecisionLog and engine journals: append-only JSONL, one file per Tehran day, flushed on every write.

`decisions-*.jsonl` holds every trading decision (entry, rejection, fill, exit, stop, take-profit, flatten,
kill, block, reconcile mismatch) with its inputs, signals, RiskGuard reason (which carries the LLM
provenance), order client ids and the config hash. `http-*.jsonl` holds every order request/response.
No secret ever enters these files: request bodies carry symbol/side/price/quantity/client_id only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")


class Journal:
    def __init__(self, directory: Path, mode: str, config_hash: str) -> None:
        self.directory = directory
        self.mode = mode
        self.config_hash = config_hash
        directory.mkdir(parents=True, exist_ok=True)

    def write(self, stream: str, payload: dict) -> dict:
        now = datetime.now(UTC)
        entry = {"time": now.isoformat(), "mode": self.mode, "config_hash": self.config_hash, **payload}
        path = self.directory / f"{stream}-{now.astimezone(TEHRAN):%Y%m%d}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, default=str, ensure_ascii=False) + "\n")
        return entry

    def decision(self, kind: str, **fields) -> dict:
        return self.write("decisions", {"kind": kind, **fields})
