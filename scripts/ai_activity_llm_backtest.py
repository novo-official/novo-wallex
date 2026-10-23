"""The live AI activity config WITH the LLM roles on history (blind replay, local model), vs the ML-only proxy.

Last 7 days (runtime: one local-model call per Officer block of 4 h and per Trade Agent candidate), measured fees
(TMN maker 0.25% / taker 0.30%). Answers are cached in data/llm_cache, so a second run replays without a model.
Usage: .venv/bin/python scripts/ai_activity_llm_backtest.py [--llm live-local|replay] [--days 7]
"""

import argparse
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
from backtest.experiments import log_experiment  # noqa: E402
from backtest.metrics import compute_metrics  # noqa: E402
from backtest.report import write_json  # noqa: E402
from execution.runner import live_limits, live_strategy_params, load_live_config  # noqa: E402
from ml.p3_experiments import TARGETS, build_market  # noqa: E402
from strategy.factory import build_strategy  # noqa: E402

MEASURED = Fees(Decimal("0.0025"), Decimal("0.0030"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", default="live-local", choices=["live-local", "replay"])
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    cfg = load_study_config(settings.BASE_DIR / "config/strategy.toml", settings.BASE_DIR)
    live = load_live_config()
    target = next(t for t in TARGETS if t.name == "h24")
    data, _ = build_market(cfg, settings.BASE_DIR / "data", [target], ["USDTTMN", "BTCTMN", "ETHTMN"])
    end = data.clock[-1]
    start = end - pd.Timedelta(days=args.days)
    costs = CostModel(MEASURED, cfg.slippage, cfg.default_slippage)
    params = live_strategy_params(cfg, live)
    out, logs = {}, {}
    for name, spec in (
        ("ML only (proxy)", None),
        ("ML + Officer", {"officer": True, "llm": args.llm}),
        ("ML + Officer + Trade Agent (live config)", {"officer": True, "trade_agent": True, "llm": args.llm}),
    ):
        agent = build_agent_layer(spec, data.symbols) if spec else None
        bt = Backtester(
            data, build_strategy(params, data.symbols), costs, live_limits(live), Decimal("5000000"), agent=agent
        )
        result = bt.run(start, end)
        m = compute_metrics(result)
        rolling = result.equity.resample("1h").last().pct_change(72).dropna()
        out[name] = {k: m[k] for k in ("return", "max_drawdown", "trades", "trades_per_day", "win_rate", "fees_paid")}
        out[name]["worst_3d"] = float(rolling.min()) if len(rolling) else 0.0
        logs[name] = agent.log if agent else []
        print(
            f"{name:<42} ret {m['return'] * 100:+.2f}%  DD {m['max_drawdown'] * 100:.2f}%  trades {m['trades']} "
            f"({m['trades_per_day']:.2f}/d)  win {m['win_rate']:.0%}  fees {m['fees_paid']:,.0f}",
            flush=True,
        )
    reviews = [e for e in logs["ML + Officer + Trade Agent (live config)"] if e["role"] == "trade_agent"]
    summary = {
        "start": str(start),
        "end": str(end),
        "fees": "measured TMN 0.25/0.30",
        "llm": args.llm,
        "results": out,
        "trade_agent_reviews": len(reviews),
        "trade_agent_approved": sum(1 for e in reviews if e["approve"]),
        "officer_decisions": [e for e in logs["ML + Officer"] if e["role"] == "regime_officer"],
    }
    write_json(settings.BASE_DIR / "reports/ai_activity_llm/summary.json", summary)
    print(f"trade agent: {summary['trade_agent_approved']}/{summary['trade_agent_reviews']} approved")
    log_experiment(
        settings.BASE_DIR / "reports",
        f"AI activity live config with LLM roles, last {args.days} days (blind replay)",
        configs=3,
        holdout_evaluated=False,
        report="reports/ai_activity_llm",
        notes=f"llm={args.llm}; measured fees; officer every 4 h; recent data already seen by the fix check",
    )


if __name__ == "__main__":
    main()
