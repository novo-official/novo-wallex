"""Read-only views of the engine's own files: state, heartbeat, decision journal, reports. No secrets live there."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

MODES = ("shadow", "live")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def decisions(base: Path, mode: str, limit: int | None = None, kinds: set[str] | None = None) -> list[dict]:
    """All decision-log rows of `mode`, newest first."""
    rows = []
    for path in sorted((base / "journal" / mode).glob("decisions-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if kinds is None or row.get("kind") in kinds:
                rows.append(row)
    rows.sort(key=lambda r: r.get("time", ""), reverse=True)
    return rows[:limit] if limit else rows


def trades(rows: list[dict]) -> list[dict]:
    """Round trips from the log: each CLOSE with the FILL of the same symbol before it."""
    out, fills = [], {}
    for row in sorted(rows, key=lambda r: r.get("time", "")):
        if row.get("kind") == "FILL":
            fills[row.get("symbol")] = row
        elif row.get("kind") == "CLOSE":
            entry = fills.pop(row.get("symbol"), {})
            out.append(
                {
                    "symbol": row.get("symbol"),
                    "module": row.get("module"),
                    "opened": entry.get("time"),
                    "closed": row.get("time"),
                    "entry_price": entry.get("price"),
                    "exit_price": row.get("price"),
                    "exit_role": row.get("role"),
                    "pnl_tmn": row.get("pnl_tmn_approx"),
                }
            )
    return list(reversed(out))


def overview(base: Path, mode: str) -> dict:
    state = _read_json(base / "logs" / f"engine_state_{mode}.json") or {}
    heartbeat = None
    try:
        heartbeat = (base / "logs" / f"heartbeat_{mode}.txt").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    age = None
    if heartbeat:
        age = (datetime.now(UTC) - datetime.fromisoformat(heartbeat)).total_seconds()
    rows = decisions(base, mode)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.get("kind", "?")] = counts.get(row.get("kind", "?"), 0) + 1
    officer = next((r for r in rows if r.get("kind") == "LLM" and r.get("role") == "regime_officer"), None)
    starts = [r for r in rows if r.get("kind") == "START"]
    return {
        "mode": mode,
        "state": {k: v for k, v in state.items() if k not in ("positions", "open_orders")},
        "positions": state.get("positions", {}),
        "open_orders": state.get("open_orders", []),
        "heartbeat": heartbeat,
        "heartbeat_age_seconds": age,
        "running": age is not None and age < 120,
        "counts": counts,
        "errors": counts.get("ERROR", 0),
        "errors_since_start": sum(
            1 for r in rows if r.get("kind") == "ERROR" and starts and r.get("time", "") >= starts[0]["time"]
        ),
        "officer": officer,
        "started": starts[-1]["time"] if starts else None,
        "last_start": starts[0] if starts else None,
        "trades": trades(rows),
        "backtest": _read_json(base / "reports" / "ai_fix_check" / "summary.json"),
    }
