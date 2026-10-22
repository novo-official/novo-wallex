import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand

from backtest.config import load_study_config
from backtest.dataset import build_features
from backtest.event_study import ExitRule, simulate, summarize
from backtest.report import write_json

RULES = [
    ExitRule(0.01, 0.02, 288),
    ExitRule(0.015, 0.03, 288),
    ExitRule(0.015, 0.045, 576),
    ExitRule(0.02, 0.04, 576),
    ExitRule(0.02, 0.06, 864),
]


def signals(frame: pd.DataFrame) -> dict[str, pd.Series]:
    traded = frame["volume"] > 0
    up_1h = (frame["close_1h"] > frame["ema200_1h"]) & (frame["ema50_1h"] > frame["ema200_1h"])
    up_1h &= frame["ema50_slope_1h"] > 0
    up_4h = (frame["close_4h"] > frame["ema50_4h"]) & (frame["ema20_4h"] > frame["ema50_4h"])
    breakout = traded & (frame["close"] > frame["prior_high_12"]) & (frame["close"] > frame["ema20_15m"])
    pullback = frame["rsi14_15m"].rolling(12, min_periods=1).min() < 45
    in_range = (frame["adx14_1h"] < 20) & (frame["close_1h"] > frame["ema200_1h"])
    out = {
        "baseline_all": pd.Series(True, index=frame.index),
        "uptrend_1h": up_1h,
        "uptrend_1h_4h": up_1h & up_4h,
        "trend_breakout": up_1h & breakout,
        "trend_breakout_4h": up_1h & up_4h & breakout,
        "trend_pullback_breakout": up_1h & pullback & breakout,
        "mean_reversion": in_range & (frame["close_15m"] < frame["bb_low_15m"]) & (frame["rsi14_15m"] < 30) & traded,
    }
    if "fv_z" in frame:
        valid = frame["fv_valid"].fillna(False).astype(bool)
        out["fv_discount_z2"] = valid & (frame["fv_z"] < -2.0) & traded
        out["uptrend_1h_no_premium"] = up_1h & ~(valid & (frame["fv_z"] > 1.5))
    return out


class Command(BaseCommand):
    help = "Event study of every entry signal x exit rule x entry type, split in time into selection and holdout."

    def add_arguments(self, parser):
        parser.add_argument("--config", default=str(settings.BASE_DIR / "config" / "strategy.toml"))
        parser.add_argument("--out", default=None)

    def handle(self, *args, **options):
        config = load_study_config(Path(options["config"]), settings.BASE_DIR)
        data_dir = settings.BASE_DIR / "data"
        symbols = list(config.tradable) + [s.removesuffix("TMN") + "USDT" for s in config.tradable if s != "USDTTMN"]
        features = build_features(data_dir, symbols)
        fees = config.fees
        rows = []
        for symbol in config.tradable:
            if symbol not in features:
                continue
            frame = features[symbol]
            warmup = int(config.windows.warmup_days * 288)
            frame = frame.iloc[warmup:]
            cut = int(len(frame) * config.selection_share)
            arrays = {name: frame[name].to_numpy(float) for name in ("open", "high", "low", "close")}
            slip = float(config.costs().slippage_for(symbol))
            for name, signal in signals(frame).items():
                flags = signal.fillna(False).to_numpy(bool)
                for rule, entry in itertools.product(RULES, ("maker", "taker")):
                    for split, (start, end) in (("selection", (0, cut)), ("holdout", (cut, len(frame)))):
                        returns = simulate(
                            arrays["open"],
                            arrays["high"],
                            arrays["low"],
                            arrays["close"],
                            flags,
                            rule,
                            maker_fee=float(fees.maker),
                            taker_fee=float(fees.taker),
                            slippage=slip,
                            entry=entry,
                            start=start,
                            end=end,
                        )
                        result = summarize(returns, int(flags[start:end].sum()))
                        rows.append(
                            {
                                "symbol": symbol,
                                "signal": name,
                                "entry": entry,
                                "split": split,
                                "rule": f"stop {rule.stop:.3f} tp {rule.take_profit:.3f} max {rule.max_bars}",
                                **result.as_dict(),
                            }
                        )
            self.stdout.write(f"{symbol}: done")
        table = pd.DataFrame(rows)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = Path(options["out"]) if options["out"] else settings.BASE_DIR / "reports" / f"edge-{stamp}"
        out.mkdir(parents=True, exist_ok=True)
        table.to_csv(out / "edge.csv", index=False)

        # Pooled over symbols: the mean net return per trade, weighted by trades.
        pooled = (
            table.assign(weighted=table["mean"] * table["filled"])
            .groupby(["signal", "rule", "entry", "split"])
            .agg(filled=("filled", "sum"), weighted=("weighted", "sum"))
        )
        pooled["mean"] = pooled["weighted"] / pooled["filled"].replace(0, np.nan)
        wide = pooled["mean"].unstack("split")
        wide["filled_selection"] = pooled["filled"].unstack("split")["selection"]
        wide = wide.sort_values("selection", ascending=False)
        wide.to_csv(out / "edge_pooled.csv")
        write_json(
            out / "summary.json", {"top_selection": json.loads(wide.head(15).reset_index().to_json(orient="records"))}
        )
        self.stdout.write(wide.head(25).round(5).to_string())
        self.stdout.write(f"report: {out}")
