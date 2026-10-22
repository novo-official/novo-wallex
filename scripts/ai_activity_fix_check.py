"""AI activity fix check (user, 2026-10-23): three pre-specified variants, 14 days, MEASURED fees.

TUNED ON RECENT DATA: the variants were chosen after seeing the 14-day result of (a), on the same 14 days, so
their numbers are optimistic. ML-ONLY PROXY: no LLM approvals in the historical run. Walk-forward h24 predictions.
  (a) current live config
  (b) only candidates with E[net] > 0 (the model's E[net] is already net of costs)
  (c) (b) and no entry while close_15m < ema20_15m
Fee rows: measured (GET /v1/account/fee 2026-10-23, TMN maker 0.25% / taker 0.30%), measured x1.5, low 0.15/0.20.
"""

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
from backtest.costs import CostModel, Fees  # noqa: E402
from backtest.engine import Backtester  # noqa: E402
from backtest.experiments import log_experiment  # noqa: E402
from backtest.metrics import compute_metrics  # noqa: E402
from backtest.report import write_json  # noqa: E402
from execution.runner import live_limits, live_strategy_params, load_live_config  # noqa: E402
from ml.p3_experiments import TARGETS, build_market  # noqa: E402
from strategy.factory import build_strategy  # noqa: E402

VARIANTS = {
    "a_current": {},
    "b_edge_positive": {"min_edge": 0.0},
    "c_edge_positive_15m_up": {"min_edge": 0.0, "skip_15m_down": True},
}
MEASURED = Fees(Decimal("0.0025"), Decimal("0.0030"))


def main() -> None:
    cfg = load_study_config(settings.BASE_DIR / "config/strategy.toml", settings.BASE_DIR)
    live = load_live_config()
    target = next(t for t in TARGETS if t.name == "h24")
    data, _ = build_market(cfg, settings.BASE_DIR / "data", [target], ["USDTTMN", "BTCTMN", "ETHTMN"])
    fee_rows = {
        "measured": MEASURED,
        "measured_x1.5": Fees(MEASURED.maker * Decimal("1.5"), MEASURED.taker * Decimal("1.5")),
        "low": Fees(Decimal("0.0015"), Decimal("0.0020")),
    }
    end = data.clock[-1]
    start = end - pd.Timedelta(days=14)
    out = {}
    for name, overrides in VARIANTS.items():
        params = live_strategy_params(cfg, live)
        params["ai_activity"] = params["ai_activity"] | overrides
        for row, fees in fee_rows.items():
            costs = CostModel(fees, cfg.slippage, cfg.default_slippage)
            bt = Backtester(data, build_strategy(params, data.symbols), costs, live_limits(live), Decimal("5000000"))
            result = bt.run(start, end)
            m = compute_metrics(result)
            daily = result.equity.resample("1D").last().pct_change().dropna()
            rolling3 = result.equity.resample("1h").last().pct_change(72).dropna()
            r = {k: m[k] for k in ("return", "max_drawdown", "trades", "trades_per_day", "win_rate", "fees_paid")}
            r["mean_pnl_per_trade_tmn"] = m["return"] * 5_000_000 / max(m["trades"], 1)
            r["worst_day"] = float(daily.min()) if len(daily) else 0.0
            r["worst_3d"] = float(rolling3.min()) if len(rolling3) else 0.0
            out[f"{name}/{row}"] = r
            print(
                f"{name:<24} {row:<14} ret {m['return'] * 100:+.2f}%  DD {m['max_drawdown'] * 100:.2f}%  "
                f"trades {m['trades']} ({m['trades_per_day']:.2f}/d)  win {m['win_rate']:.0%}  "
                f"mean {r['mean_pnl_per_trade_tmn']:+,.0f}  fees {m['fees_paid']:,.0f}  "
                f"worst3d {r['worst_3d'] * 100:+.2f}%"
            )
    write_json(
        settings.BASE_DIR / "reports/ai_fix_check/summary.json",
        {"start": str(start), "end": str(end), "label": __doc__, "variants": VARIANTS, "results": out},
    )
    log_experiment(
        settings.BASE_DIR / "reports",
        "AI activity fix check (3 variants, last 14 days, measured fees)",
        configs=3,
        holdout_evaluated=False,
        report="reports/ai_fix_check",
        notes="tuned on recent data (same 14 days that showed (a)'s loss); ML-only proxy; also logs the earlier "
        "single g1_ai run of (a) at default fees (reports/g1_ai)",
    )


if __name__ == "__main__":
    main()
