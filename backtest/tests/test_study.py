import numpy as np
import pandas as pd
import pytest
from django.conf import settings

from backtest.config import MAX_GRID, config_hash, expand_grid, load_study_config
from backtest.event_study import ExitRule, simulate
from backtest.windows import Objective, WindowSpec, objective, split_selection_holdout, window_starts, window_summary


def test_grid_expands_dotted_keys_and_enforces_the_limit():
    base = {"modules": ["trend"], "trend": {"stop_atr": 2.0}}
    combos = expand_grid(base, {"trend.tp_r": [2.0, 3.0], "symbols": [["BTCTMN"], ["ETHTMN"]]})
    assert len(combos) == 4
    assert {c["trend"]["tp_r"] for c in combos} == {2.0, 3.0} and all(c["trend"]["stop_atr"] == 2.0 for c in combos)
    assert base == {"modules": ["trend"], "trend": {"stop_atr": 2.0}}  # the base is not mutated
    with pytest.raises(ValueError):
        expand_grid(base, {"trend.tp_r": list(range(MAX_GRID + 1))})


def test_repository_grid_is_within_the_limit():
    config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
    assert len(expand_grid(config.strategy, config.grid)) <= MAX_GRID


def test_config_hash_is_stable_and_sensitive():
    config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
    manifest = {"files": {"candles/1m/BTCTMN.parquet": {"sha256": "a"}}}
    first, _ = config_hash(config, config.strategy, manifest)
    again, _ = config_hash(config, dict(config.strategy), manifest)
    other, _ = config_hash(config, {**config.strategy, "symbols": ["BTCTMN"]}, manifest)
    changed_data, _ = config_hash(config, config.strategy, {"files": {"candles/1m/BTCTMN.parquet": {"sha256": "b"}}})
    assert first == again and len({first, other, changed_data}) == 3


def test_windows_start_every_12h_after_warmup_and_holdout_does_not_overlap_selection():
    clock = pd.date_range("2026-01-01T00:00:00Z", periods=288 * 60, freq="5min")
    spec = WindowSpec()
    starts = window_starts(clock, spec)
    assert starts[0] >= clock[0] + pd.Timedelta(days=10)
    assert all(b - a == pd.Timedelta(hours=12) for a, b in zip(starts, starts[1:], strict=False))
    assert starts[-1] + pd.Timedelta(days=3) <= clock[-1]
    selection, holdout = split_selection_holdout(starts, spec)
    assert holdout[0] >= selection[-1] + pd.Timedelta(days=3)


def test_objective_requires_both_constraints():
    rows = pd.DataFrame(
        {"return": [0.01, 0.02, -0.01], "max_drawdown": [-0.01] * 3, "trades": [1] * 3, "fees_share": [0.001] * 3}
    )
    summary = window_summary(rows)
    assert summary["positive_share"] == pytest.approx(2 / 3)
    assert objective(summary) == pytest.approx(0.01)
    assert objective(summary, min_positive=0.9) == float("-inf")
    assert objective({**summary, "p10_drawdown": -0.05}) == float("-inf")


def test_event_simulation_take_profit_and_stop_with_costs():
    close = np.array([100.0, 100, 100, 100, 100, 100])
    open_ = close.copy()
    high = np.array([100.0, 100, 103, 100, 100, 100])
    low = np.array([100.0, 99.9, 100, 100, 100, 100])
    signal = np.array([True, False, False, False, False, False])
    rule = ExitRule(stop=0.01, take_profit=0.02, max_bars=10)
    (net,) = simulate(open_, high, low, close, signal, rule, maker_fee=0.0025, taker_fee=0.0035, slippage=0.0)
    entry = 100 * (1 - 0.0005)
    assert net == pytest.approx(entry * 1.02 * (1 - 0.0025) / (entry * 1.0025) - 1)

    low_stop = np.array([100.0, 99.9, 98.0, 100, 100, 100])
    high_both = np.array([100.0, 100, 103, 100, 100, 100])
    (net,) = simulate(open_, high_both, low_stop, close, signal, rule, maker_fee=0.0025, taker_fee=0.0035, slippage=0.0)
    assert net < -0.01  # stop first when both are in one bar


def test_new_objective_counts_zero_trade_windows_as_failures_and_requires_activity():
    rows = pd.DataFrame(
        {
            "return": [0.003, 0.0, 0.01, -0.001],
            "max_drawdown": [-0.01] * 4,
            "trades": [10, 0, 12, 14],
            "fees_share": [0.001] * 4,
        }
    )
    summary = window_summary(rows)
    assert summary["p_above_target"] == pytest.approx(0.5)  # the zero-trade window is a failure
    assert summary["trades_per_day"] == pytest.approx(36 / 4 / 3)
    assert Objective().score(summary) == pytest.approx(0.5)
    assert Objective(min_trades_per_day=4).score(summary) == float("-inf")
    assert Objective().score({**summary, "p10_drawdown": -0.036}) == float("-inf")


def test_fee_table_has_low_base_and_stress_rows():
    config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
    cases = config.cost_cases()
    assert list(cases) == ["low", "base", "stress"]
    assert str(cases["low"].fees.maker) == "0.0015" and str(cases["base"].fees.taker) == "0.0035"
    assert cases["stress"].fees.taker == cases["base"].fees.taker * 3 / 2


def test_experiment_refuses_more_than_30_configs(tmp_path):
    from backtest.experiment import MAX_CONFIGS, run_experiment

    config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
    with pytest.raises(ValueError, match="at most"):
        run_experiment("too big", config, None, {}, [("v", {})] * (MAX_CONFIGS + 1), tmp_path, tmp_path)


def test_canonical_strategy_has_activity_trades_and_quota_is_shadow_only(tmp_path):
    from backtest.experiment import run_experiment

    config = load_study_config(settings.BASE_DIR / "config" / "strategy.toml", settings.BASE_DIR)
    assert config.strategy["modules"] == ["trend", "ml_signal"]
    assert config.strategy["ml_signal"]["size_scale"] == 0.1 and config.strategy["risk"]["min_order_tmn"] == 50000
    quota = config.shadow_variants["activity_quota"]
    assert quota["ml_signal"]["max_entries_per_day"] == 2 and quota["ml_signal"]["threshold"] == -0.005
    with pytest.raises(ValueError, match="shadow-only"):
        run_experiment("quota", config, None, {}, [("quota", quota)], tmp_path, tmp_path)
