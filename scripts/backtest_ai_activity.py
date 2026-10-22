"""14-day / 7-day backtest of the exact live AI activity configuration (config/live.toml [ai_activity]).

ML-ONLY PROXY: the historical run has no LLM approvals (the Officer/Trade Agent would only remove trades), so every
ML-ranked candidate that RiskGuard accepts is taken. ML predictions are walk-forward (out of fold), not the live
model. Usage: .venv/bin/python scripts/backtest_ai_activity.py [--days 14 7]
"""

import argparse
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

from backtest.config import load_study_config  # noqa: E402
from backtest.engine import Backtester  # noqa: E402
from backtest.metrics import compute_metrics  # noqa: E402
from backtest.report import write_json  # noqa: E402
from execution.runner import live_limits, live_strategy_params, load_live_config  # noqa: E402
from ml.p3_experiments import TARGETS, build_market  # noqa: E402
from strategy.factory import build_strategy  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, nargs="+", default=[14, 7])
    parser.add_argument("--out", default="reports/g1_ai/summary.json")
    args = parser.parse_args()
    cfg = load_study_config(settings.BASE_DIR / "config/strategy.toml", settings.BASE_DIR)
    live = load_live_config()
    target = next(t for t in TARGETS if t.name == "h24")
    data, _ = build_market(cfg, settings.BASE_DIR / "data", [target], ["USDTTMN", "BTCTMN", "ETHTMN"])
    params = live_strategy_params(cfg, live)
    limits = live_limits(live)
    end = data.clock[-1]
    out = {}
    for days in args.days:
        start = end - pd.Timedelta(days=days)
        for case, costs in cfg.cost_cases().items():
            result = Backtester(data, build_strategy(params, data.symbols), costs, limits, Decimal("5000000")).run(
                start, end
            )
            m = compute_metrics(result)
            trades = m["trades"] or 1
            row = {k: m[k] for k in ("return", "max_drawdown", "trades", "trades_per_day", "win_rate", "fees_paid")}
            row["mean_pnl_per_trade_tmn"] = m["return"] * 5_000_000 / trades
            out[f"{days}d/{case}"] = row
            print(
                f"{days:>2}d {case:<6} return {m['return'] * 100:+.2f}%  maxDD {m['max_drawdown'] * 100:.2f}%  "
                f"trades {m['trades']} ({m['trades_per_day']:.2f}/day)  win {m['win_rate']:.0%}  "
                f"mean/trade {row['mean_pnl_per_trade_tmn']:+,.0f} TMN  fees {m['fees_paid']:,.0f}"
            )
    print("period end", end)
    write_json(
        settings.BASE_DIR / args.out,
        {
            "end": str(end),
            "label": "ML-only proxy (no LLM approvals), walk-forward h24",
            "params": params,
            "results": out,
        },
    )
    print(json.dumps({"written": args.out}))


if __name__ == "__main__":
    main()
