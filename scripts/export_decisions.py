"""Export the engine's decision logs for the deliverable: one JSONL (verbatim rows) and one CSV per mode.

Usage: .venv/bin/python scripts/export_decisions.py [--mode shadow] [--out reports/decision_logs]
Rows are the engine's own journal (journal/<mode>/decisions-*.jsonl): every candidate, LLM decision, RiskGuard
verdict, order, fill, exit and error, each with the config hash + code version that produced it.
"""

import argparse
import csv
import json
from pathlib import Path

COLUMNS = [
    "time",
    "kind",
    "mode",
    "symbol",
    "module",
    "role",
    "risk_mode",
    "reason",
    "risk",
    "rationale",
    "approve",
    "size_fraction",
    "limit",
    "price",
    "quantity",
    "stop",
    "take_profit",
    "fee",
    "fee_asset",
    "pnl_tmn_approx",
    "client_id",
    "status",
    "error",
    "config_hash",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="shadow")
    parser.add_argument("--out", default="reports/decision_logs")
    args = parser.parse_args()
    rows = []
    for path in sorted(Path("journal", args.mode).glob("decisions-*.jsonl")):
        rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.sort(key=lambda r: r.get("time", ""))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{args.mode}_decisions.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    with (out / f"{args.mode}_decisions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()}
            )
    kinds = {}
    for row in rows:
        kinds[row.get("kind")] = kinds.get(row.get("kind"), 0) + 1
    print(json.dumps({"mode": args.mode, "rows": len(rows), "kinds": kinds, "out": str(out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
