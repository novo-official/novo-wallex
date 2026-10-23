from datetime import datetime
from zoneinfo import ZoneInfo

from execution.runner import challenge_end


def test_env_challenge_end_wins_over_config_and_defaults_to_tehran(settings):
    live = {"challenge": {"end": "2026-10-30T12:00:00+03:30"}}
    settings.CHALLENGE_END = "2026-10-26T12:00:00"
    assert challenge_end(live) == datetime(2026, 10, 26, 12, tzinfo=ZoneInfo("Asia/Tehran"))
    settings.CHALLENGE_END = ""
    assert challenge_end(live).isoformat() == "2026-10-30T12:00:00+03:30"
    assert challenge_end({"challenge": {"end": ""}}) is None


def test_smc_runs_alongside_ai_activity_with_its_own_columns(settings):
    import numpy as np
    import pandas as pd

    from backtest.config import load_study_config
    from execution.runner import live_strategy_params, load_live_config, smc_columns, smc_settings
    from strategy.factory import build_strategy

    live = load_live_config()
    study = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
    params = live_strategy_params(study, live)
    assert params["modules"] == ["ai_activity", "smc_long"]
    strategy = build_strategy(params, ["BTCTMN", "ETHTMN", "USDTTMN"])
    assert [m.name for m in strategy.modules] == ["ai_activity", "smc_long"]
    smc_params, module = smc_settings(live)
    assert smc_params.pivot_n == 2 and smc_params.zone == "fvg_or_ob" and module["timeout_bars"] == 24

    index = pd.date_range("2026-10-20", periods=3000, freq="1min", tz="UTC")
    close = pd.Series(100 + np.cumsum(np.random.default_rng(3).normal(0, 0.2, len(index))), index=index)
    bars = pd.DataFrame(
        {"open": close.shift(1).fillna(100), "high": close + 0.1, "low": close - 0.1, "close": close, "volume": 1.0},
        index=index,
    )

    class Feed:
        def __init__(self):
            self.bars = {"BTCTMN": bars}

        def _frontier(self):
            return bars.index[-1]

        def _extended(self, frame, frontier):
            return frame

    clock = pd.date_range(index[0] + pd.Timedelta(minutes=5), index[-1], freq="5min", tz="UTC")
    out = smc_columns(Feed(), smc_params)({"BTCTMN": pd.DataFrame(index=clock)})
    assert {"setup_id_smc15", "structure_smc1h", "fvg_dist_atr_smc15"} <= set(out["BTCTMN"])
    assert len(out["BTCTMN"]["structure_smc1h"]) == len(clock)
