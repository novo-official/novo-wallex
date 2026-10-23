"""Blind historical replay of the live config (AI activity + both LLM roles) as a decision journal for the dashboard.

Real model answers from data/llm_cache (no new calls), last 7 days of committed data, measured fees. Written to
journal/replay/ with mode "replay", so it is never mixed with shadow or live. Shows what shadow has not (yet):
Officer DEFENSIVE decisions and RiskGuard applying them. Usage: .venv/bin/python scripts/export_replay_journal.py
"""

import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, ".")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import pandas as pd  # noqa: E402
from django.conf import settings  # noqa: E402

from agent.replay import build_agent_layer  # noqa: E402
from backtest.config import load_study_config  # noqa: E402
from backtest.costs import CostModel, Fees  # noqa: E402
from backtest.engine import Backtester  # noqa: E402
from execution.runner import live_limits, live_strategy_params, load_live_config  # noqa: E402
from ml.p3_experiments import TARGETS, build_market  # noqa: E402
from strategy.factory import build_strategy  # noqa: E402

TAG = "replay-7d-live-config"


def main() -> None:
    cfg = load_study_config(settings.BASE_DIR / "config/strategy.toml", settings.BASE_DIR)
    live = load_live_config()
    target = next(t for t in TARGETS if t.name == "h24")
    data, _ = build_market(cfg, settings.BASE_DIR / "data", [target], ["USDTTMN", "BTCTMN", "ETHTMN"])
    end = data.clock[-1]
    start = end - pd.Timedelta(days=7)
    agent = build_agent_layer({"officer": True, "trade_agent": True, "llm": "replay"}, data.symbols)
    costs = CostModel(Fees(Decimal("0.0025"), Decimal("0.0030")), cfg.slippage, cfg.default_slippage)
    bt = Backtester(
        data,
        build_strategy(live_strategy_params(cfg, live), data.symbols),
        costs,
        live_limits(live),
        Decimal("5000000"),
        agent=agent,
    )
    bt.record_decisions = True
    result = bt.run(start, end)
    rows = []
    base = {"mode": "replay", "config_hash": TAG}
    for d in result.decisions:
        rows.append(
            {
                **base,
                "time": d["time"],
                "kind": d["action"],
                "symbol": d["symbol"],
                "module": d["module"],
                "reason": d["reason"],
                "risk": d["risk"],
                "signals": d["signals"],
            }
        )
    for e in agent.log:
        row = {k: v for k, v in e.items() if k != "time"}
        rows.append({**base, "time": e["time"], "decided_at": e["time"], "kind": "LLM", **row})
    for t in result.trades:
        rows.append(
            {
                **base,
                "time": t.entry_time.isoformat(),
                "kind": "FILL",
                "symbol": t.symbol,
                "module": t.module,
                "price": str(t.entry_price),
                "quantity": str(t.quantity),
            }
        )
        rows.append(
            {
                **base,
                "time": t.exit_time.isoformat(),
                "kind": "CLOSE",
                "symbol": t.symbol,
                "module": t.module,
                "price": str(t.exit_price),
                "role": t.exit_reason,
                "pnl_tmn_approx": str(t.pnl),
            }
        )
    rows.sort(key=lambda r: r["time"])
    out = settings.BASE_DIR / "journal" / "replay" / "decisions-replay-7d.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    kinds: dict[str, int] = {}
    for row in rows:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    print(json.dumps({"rows": len(rows), "kinds": kinds, "return": float(result.final_equity / 5_000_000 - 1)}))


if __name__ == "__main__":
    main()
