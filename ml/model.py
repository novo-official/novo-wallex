"""LightGBM classifier with isotonic calibration, purged walk-forward predictions, and versioned saving.

Purging: a training sample is used for a test block only if its label was resolved (`end`)
before the block starts, so no training label overlaps the period it is evaluated on.
Calibration: the booster is trained on the earliest (1 − calibration_share) of the purged
training samples and an isotonic map is fitted on the latest calibration_share (also purged
from the booster's part by one label horizon), so probabilities are honest out of sample.
Hyperparameters are fixed and conservative (shallow trees, large leaves); they are not tuned.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

LGBM_PARAMS = {
    "objective": "binary",
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbose": -1,
    "num_threads": 4,
    "seed": 7,
    "deterministic": True,
    "force_col_wise": True,
}
NUM_ROUNDS = 300


@dataclass(frozen=True)
class WalkForwardSpec:
    min_train_days: float = 45.0
    block_days: float = 14.0
    calibration_share: float = 0.2
    sample_every: int = 6  # one training sample per 30 min: labels overlap heavily anyway


@dataclass
class Samples:
    """Training rows: features X, label y, decision time t, label resolution time t_end, symbol."""

    X: pd.DataFrame
    y: np.ndarray
    net: np.ndarray
    t: pd.DatetimeIndex
    t_end: pd.DatetimeIndex
    symbol: np.ndarray


@dataclass
class CalibratedModel:
    booster: lgb.Booster
    calibrator: IsotonicRegression | None
    features: list[str]
    meta: dict = field(default_factory=dict)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        raw = self.booster.predict(X[self.features].to_numpy(dtype=float))
        if self.calibrator is None:
            return raw
        return self.calibrator.predict(raw)


def fit(samples: Samples, horizon: pd.Timedelta, calibration_share: float) -> CalibratedModel:
    order = np.argsort(samples.t.asi8)
    t = samples.t[order]
    cut_time = t[int(len(t) * (1 - calibration_share))]
    fit_mask = samples.t_end < cut_time  # booster labels resolved before the calibration period
    calibration_mask = samples.t >= cut_time
    X = samples.X.to_numpy(dtype=float)
    dataset = lgb.Dataset(X[fit_mask], label=samples.y[fit_mask], feature_name=list(samples.X.columns))
    booster = lgb.train(LGBM_PARAMS, dataset, num_boost_round=NUM_ROUNDS)
    calibrator = None
    if calibration_mask.sum() >= 200 and len(np.unique(samples.y[calibration_mask])) == 2:
        calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        calibrator.fit(booster.predict(X[calibration_mask]), samples.y[calibration_mask])
    return CalibratedModel(
        booster=booster,
        calibrator=calibrator,
        features=list(samples.X.columns),
        meta={
            "fit_rows": int(fit_mask.sum()),
            "calibration_rows": int(calibration_mask.sum()),
            "base_rate": float(samples.y.mean()),
        },
    )


def walk_forward(
    samples: Samples,
    predict_frames: dict[str, pd.DataFrame],
    spec: WalkForwardSpec,
    horizon: pd.Timedelta,
) -> tuple[dict[str, pd.Series], pd.DataFrame]:
    """Out-of-sample probabilities for every bar of `predict_frames`, block by block.

    Each block [a, a + block) is predicted by a model trained on samples resolved before a.
    Bars before the first block get NaN. Returns (edge per symbol, per-block metrics), where
    edge = calibrated probability − the base rate of that block's training labels.
    """
    first = samples.t.min() + pd.Timedelta(days=spec.min_train_days)
    last = max(frame.index[-1] for frame in predict_frames.values())
    probabilities = {symbol: pd.Series(np.nan, index=frame.index) for symbol, frame in predict_frames.items()}
    rows = []
    block_start = first.ceil("1D")
    while block_start <= last:
        block_end = block_start + pd.Timedelta(days=spec.block_days)
        train = samples.t_end < block_start
        model = fit(_subset(samples, train), horizon, spec.calibration_share)
        for symbol, frame in predict_frames.items():
            in_block = (frame.index >= block_start) & (frame.index < block_end)
            if in_block.any():
                predicted = model.predict(frame.loc[in_block]) - model.meta["base_rate"]
                probabilities[symbol].iloc[np.flatnonzero(in_block)] = predicted
        test = (samples.t >= block_start) & (samples.t < block_end)
        rows.append(
            {
                "block_start": block_start,
                "train_rows": int(train.sum()),
                "train_base_rate": model.meta["base_rate"],
                **_metrics(samples, test, model),
            }
        )
        block_start = block_end
    return probabilities, pd.DataFrame(rows)


def _subset(samples: Samples, mask: np.ndarray) -> Samples:
    return Samples(
        X=samples.X[mask],
        y=samples.y[mask],
        net=samples.net[mask],
        t=samples.t[mask],
        t_end=samples.t_end[mask],
        symbol=samples.symbol[mask],
    )


def _metrics(samples: Samples, mask: np.ndarray, model: CalibratedModel) -> dict:
    if mask.sum() < 50 or len(np.unique(samples.y[mask])) < 2:
        return {"test_rows": int(mask.sum())}
    y = samples.y[mask]
    p = np.clip(model.predict(samples.X[mask]), 1e-6, 1 - 1e-6)
    base = np.full_like(p, y.mean())
    return {
        "test_rows": int(mask.sum()),
        "base_rate": float(y.mean()),
        "auc": float(roc_auc_score(y, p)),
        "brier": float(brier_score_loss(y, p)),
        "brier_base": float(brier_score_loss(y, base)),
        "logloss": float(log_loss(y, p)),
        "logloss_base": float(log_loss(y, base)),
    }


def calibration_table(y: np.ndarray, p: np.ndarray, bins: int = 5) -> pd.DataFrame:
    frame = pd.DataFrame({"y": y, "p": p}).dropna()
    frame["bin"] = pd.qcut(frame["p"], bins, duplicates="drop")
    return frame.groupby("bin", observed=True).agg(predicted=("p", "mean"), actual=("y", "mean"), rows=("y", "size"))


def save(model: CalibratedModel, directory: Path, metadata: dict) -> str:
    """Write model.txt (LightGBM), calibration.json and metadata.json; returns the model id (content hash)."""
    directory.mkdir(parents=True, exist_ok=True)
    booster_text = model.booster.model_to_string()
    calibration = (
        {"x": model.calibrator.X_thresholds_.tolist(), "y": model.calibrator.y_thresholds_.tolist()}
        if model.calibrator is not None
        else None
    )
    digest = hashlib.sha256((booster_text + json.dumps(calibration)).encode()).hexdigest()[:16]
    (directory / "model.txt").write_text(booster_text, encoding="utf-8")
    (directory / "calibration.json").write_text(json.dumps(calibration) + "\n", encoding="utf-8")
    payload = {"model_id": digest, "features": model.features, **model.meta, **metadata}
    (directory / "metadata.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return digest


def load(directory: Path) -> CalibratedModel:
    booster = lgb.Booster(model_str=(directory / "model.txt").read_text(encoding="utf-8"))
    calibration = json.loads((directory / "calibration.json").read_text(encoding="utf-8"))
    calibrator = None
    if calibration is not None:
        # No y bounds: the per-bar models calibrate to net returns, which are negative (clipping to [0, 1]
        # turned every negative E[net] into 0). Refitting on the saved thresholds reproduces the map exactly.
        calibrator = IsotonicRegression(out_of_bounds="clip")
        calibrator.fit(calibration["x"], calibration["y"])
    meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    return CalibratedModel(booster=booster, calibrator=calibrator, features=meta["features"], meta=meta)


def spec_dict(spec) -> dict:
    return asdict(spec)
