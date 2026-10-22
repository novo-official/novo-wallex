"""Per-bar ML: one sample per 5m bar of the traded markets, predicting the net value of a trade.

Targets (one per experiment row; the evidence picks one):
  * horizon h (12/24/48 bars = 1h/2h/4h): net return of buying at close_t as a maker and selling
    at close_{t+h} as a taker, after slippage and both fees (regression, LightGBM L2);
  * triple barrier: stop / target / max bars with the backtester's fill rules (classification).

Both models end in the same unit: an isotonic map from the model score to the *realized net
return* of that trade, fitted on the latest part of the purged training data. The output column
`ml_net_<target>` is therefore a calibrated expected net return (E[net]), and the ML-only
module trades when E[net] exceeds a threshold. Training samples are purged (label resolved
before the block) and embargoed (and before block start − embargo).
"""

from __future__ import annotations

from dataclasses import dataclass

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.isotonic import IsotonicRegression

from .labels import BarrierSpec, triple_barrier
from .model import LGBM_PARAMS, NUM_ROUNDS

BAR = pd.Timedelta(minutes=5)


@dataclass(frozen=True)
class Target:
    name: str
    kind: str  # "horizon" or "barrier"
    horizon: int  # bars until the label is resolved at the latest
    stop: float = 0.0
    take_profit: float = 0.0


@dataclass(frozen=True)
class PerBarSpec:
    min_train_days: float = 45.0
    block_days: float = 14.0
    calibration_share: float = 0.2
    sample_every: int = 1
    embargo_bars: int = 288  # one day between the last training label and the block
    maker_fee: float = 0.0025
    taker_fee: float = 0.0035
    entry_offset: float = 0.0005


def horizon_net(close: np.ndarray, horizon: int, slippage: float, spec: PerBarSpec) -> np.ndarray:
    entry = close * (1 - spec.entry_offset) * (1 + spec.maker_fee)
    exit_value = np.full_like(close, np.nan)
    exit_value[:-horizon] = close[horizon:] * (1 - slippage) * (1 - spec.taker_fee)
    return exit_value / entry - 1


def labels_for(frame: pd.DataFrame, target: Target, slippage: float, spec: PerBarSpec):
    """(y for the model, realized net return, resolution index) for every bar of `frame`."""
    close = frame["close"].to_numpy(float)
    n = len(close)
    if target.kind == "horizon":
        net = horizon_net(close, target.horizon, slippage, spec)
        end = np.where(np.arange(n) + target.horizon < n, np.arange(n) + target.horizon, -1)
        return net, net, end
    barrier = BarrierSpec(
        stop=target.stop,
        take_profit=target.take_profit,
        horizon=target.horizon,
        entry_offset=spec.entry_offset,
        maker_fee=spec.maker_fee,
        taker_fee=spec.taker_fee,
    )
    net, label, end = triple_barrier(
        frame["high"].to_numpy(float), frame["low"].to_numpy(float), close, np.arange(n), barrier, slippage
    )
    return label.astype(float), net, end


def build_table(
    features: dict[str, pd.DataFrame],
    frames: dict[str, pd.DataFrame],
    symbols: list[str],
    target: Target,
    slippage: dict[str, float],
    spec: PerBarSpec,
    warmup_bars: int,
) -> pd.DataFrame:
    parts = []
    for symbol in symbols:
        frame = features[symbol]
        y, net, end = labels_for(frame, target, slippage.get(symbol, 0.002), spec)
        index = np.arange(len(frame))
        keep = (index >= warmup_bars) & (end >= 0) & (index % spec.sample_every == 0) & np.isfinite(net)
        part = frames[symbol].iloc[index[keep]].copy()
        part["t"] = frame.index[index[keep]]
        part["t_end"] = frame.index[end[keep]]
        part["y"] = y[keep]
        part["net"] = net[keep]
        part["symbol"] = symbol
        parts.append(part)
    return pd.concat(parts).sort_values("t").reset_index(drop=True)


class NetModel:
    """LightGBM (regression or binary) + isotonic map from score to realized net return."""

    def __init__(self, booster: lgb.Booster, calibrator: IsotonicRegression, features: list[str]) -> None:
        self.booster = booster
        self.calibrator = calibrator
        self.features = features

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return self.booster.predict(X[self.features].to_numpy(dtype=float))

    def expected_net(self, X: pd.DataFrame) -> np.ndarray:
        return self.calibrator.predict(self.score(X))


def fit_net_model(table: pd.DataFrame, features: list[str], target: Target, spec: PerBarSpec) -> NetModel:
    t = pd.DatetimeIndex(table["t"])
    cut = t[int(len(t) * (1 - spec.calibration_share))]
    fit_part = table[table["t_end"] < cut]
    calibration_part = table[table["t"] >= cut]
    params = dict(LGBM_PARAMS)
    params["objective"] = "regression" if target.kind == "horizon" else "binary"
    if target.kind == "horizon":
        params["min_data_in_leaf"] = 500  # noisier target: bigger leaves
    dataset = lgb.Dataset(
        fit_part[features].to_numpy(float), label=fit_part["y"].to_numpy(float), feature_name=features
    )
    booster = lgb.train(params, dataset, num_boost_round=NUM_ROUNDS)
    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(booster.predict(calibration_part[features].to_numpy(float)), calibration_part["net"].to_numpy(float))
    return NetModel(booster, calibrator, features)


def walk_forward_net(
    table: pd.DataFrame,
    predict_frames: dict[str, pd.DataFrame],
    features: list[str],
    target: Target,
    spec: PerBarSpec,
) -> tuple[dict[str, pd.Series], pd.DataFrame]:
    """Out-of-sample E[net] for every bar of `predict_frames`; per-block training stats."""
    first = pd.Timestamp(table["t"].min()) + pd.Timedelta(days=spec.min_train_days)
    last = max(frame.index[-1] for frame in predict_frames.values())
    out = {symbol: pd.Series(np.nan, index=frame.index) for symbol, frame in predict_frames.items()}
    rows = []
    block_start = first.ceil("1D")
    embargo = spec.embargo_bars * BAR
    while block_start <= last:
        block_end = block_start + pd.Timedelta(days=spec.block_days)
        train = table[table["t_end"] < block_start - embargo]
        model = fit_net_model(train, features, target, spec)
        for symbol, frame in predict_frames.items():
            mask = (frame.index >= block_start) & (frame.index < block_end)
            if mask.any():
                out[symbol].iloc[np.flatnonzero(mask)] = model.expected_net(frame.loc[mask])
        rows.append({"block_start": block_start, "train_rows": len(train)})
        block_start = block_end
    return out, pd.DataFrame(rows)


def oof_quality(table: pd.DataFrame, predictions: dict[str, pd.Series], until: pd.Timestamp) -> dict:
    """Selection-period quality of the out-of-sample E[net] (nothing after `until` is looked at)."""
    lookup = []
    for symbol, series in predictions.items():
        part = table[(table["symbol"] == symbol) & (table["t"] < until)]
        lookup.append(part.assign(pred=series.reindex(pd.DatetimeIndex(part["t"])).to_numpy()))
    joined = pd.concat(lookup).dropna(subset=["pred"])
    if joined.empty:
        return {}
    ic = spearmanr(joined["pred"], joined["net"]).statistic
    by_day = joined.groupby(pd.DatetimeIndex(joined["t"]).floor("1D"))
    daily_ic = by_day.apply(lambda g: spearmanr(g["pred"], g["net"]).statistic if len(g) > 30 else np.nan).dropna()
    positive = joined[joined["pred"] > 0]
    deciles = pd.qcut(joined["pred"].rank(method="first"), 10, labels=False)
    return {
        "rows": len(joined),
        "spearman_ic": float(ic),
        "daily_ic_mean": float(daily_ic.mean()),
        "daily_ic_positive_share": float((daily_ic > 0).mean()),
        "share_pred_positive": float(len(positive) / len(joined)),
        "mean_net_pred_positive": float(positive["net"].mean()) if len(positive) else None,
        "mean_net_all": float(joined["net"].mean()),
        "mean_net_top_decile": float(joined.loc[deciles == 9, "net"].mean()),
        "mean_net_bottom_decile": float(joined.loc[deciles == 0, "net"].mean()),
    }


def shap_summary(model: NetModel, X: pd.DataFrame, sample: int = 20000, seed: int = 7) -> pd.DataFrame:
    """Mean |SHAP| per feature (LightGBM's exact TreeSHAP via pred_contrib) and the sign of its effect."""
    rows = X.sample(min(sample, len(X)), random_state=seed)
    values = model.booster.predict(rows[model.features].to_numpy(float), pred_contrib=True)[:, :-1]
    frame = pd.DataFrame(values, columns=model.features)
    correlation = [
        float(np.corrcoef(rows[name].rank().fillna(0), frame[name])[0, 1]) if frame[name].std() > 0 else 0.0
        for name in model.features
    ]
    return (
        pd.DataFrame(
            {"feature": model.features, "mean_abs_shap": frame.abs().mean().to_numpy(), "direction": correlation}
        )
        .sort_values("mean_abs_shap", ascending=False)
        .reset_index(drop=True)
    )
