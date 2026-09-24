"""
c2t/rf.py — random forest helpers ported from cloud2trees R/utils_rf.R.

R used randomForest::tuneRF to pick mtry by OOB error, then randomForest; for large data it tuned on
subsamples and averaged several models fit on 20,000-row subsamples. Same shape here with
sklearn.ensemble.RandomForestRegressor (max_features plays the role of mtry).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor


def _mtry_sequence(p: int, step_factor: float = 1.0) -> list[int]:
    """tuneRF starts at floor(p/3) for regression and walks by step_factor. With step 1 it stays put."""
    start = max(1, int(p / 3))
    seq = {start}
    if step_factor and step_factor != 1:
        m = start
        while (m := int(round(m * step_factor))) <= p and m not in seq:
            seq.add(m)
        m = start
        while (m := int(round(m / step_factor))) >= 1 and m not in seq:
            seq.add(m)
    return sorted(seq)


def _tune_once(X: np.ndarray, y: np.ndarray, ntree: int, step_factor: float, improve: float, seed: int) -> int:
    p = X.shape[1]
    best_m, best_err = None, np.inf
    for m in _mtry_sequence(p, step_factor):
        rf = RandomForestRegressor(n_estimators=ntree, max_features=m, oob_score=True, n_jobs=-1, random_state=seed, bootstrap=True)
        rf.fit(X, y)
        err = float(np.mean((rf.oob_prediction_ - y) ** 2))
        if best_m is None or err < best_err * (1 - improve):
            best_m, best_err = m, err
    return int(best_m)


def rf_tune_subsample(predictors, response, threshold: int = 14444, n_subsamples: int = 4, ntree_try: int = 44,
                      step_factor: float = 1.0, improve: float = 0.03, seed: int = 21) -> int:
    """Return the mtry (max_features) to use, tuned on the data or on n_subsamples subsamples when n > threshold."""
    X = np.asarray(pd.DataFrame(predictors).astype(float).values)
    y = np.asarray(response, dtype=float)
    ok = np.isfinite(X).all(axis=1) & np.isfinite(y)
    X, y = X[ok], y[ok]
    max_ntree_try = 122
    rng = np.random.default_rng(seed)
    if len(y) <= threshold:
        m = _tune_once(X, y, max_ntree_try, step_factor, improve, seed)
    else:
        picks = []
        for i in range(n_subsamples):
            idx = rng.choice(len(y), size=threshold, replace=False)
            picks.append(_tune_once(X[idx], y[idx], min(ntree_try, max_ntree_try), step_factor, improve, seed + i))
        vals, counts = np.unique(picks, return_counts=True)
        m = int(vals[np.argmax(counts)])
    return int(min(m, X.shape[1]))


def rf_tune_model(predictors, response, ntree: int = 500, seed: int = 21, **tune) -> RandomForestRegressor:
    X = pd.DataFrame(predictors).astype(float)
    y = np.asarray(response, dtype=float)
    ok = np.isfinite(X.values).all(axis=1) & np.isfinite(y)
    X, y = X[ok], y[ok]
    m = rf_tune_subsample(X, y, seed=seed, **tune)
    rf = RandomForestRegressor(n_estimators=ntree, max_features=m, oob_score=True, n_jobs=-1, random_state=seed)
    rf.fit(X.values, y)
    rf.feature_names_ = list(X.columns)
    return rf


def rf_subsample_and_model_n_times(predictors, response, mod_n_subsample: int = 20000, mod_n_times: int = 3,
                                   ntree: int = 500, seed: int = 21, **tune) -> list[RandomForestRegressor]:
    """One model when n <= mod_n_subsample, otherwise mod_n_times models each fit on a random subsample."""
    X = pd.DataFrame(predictors).astype(float).reset_index(drop=True)
    y = np.asarray(response, dtype=float)
    if len(y) <= mod_n_subsample:
        return [rf_tune_model(X, y, ntree=ntree, seed=seed, **tune)]
    rng = np.random.default_rng(seed)
    mods = []
    for i in range(mod_n_times):
        idx = rng.choice(len(y), size=mod_n_subsample, replace=False)
        mods.append(rf_tune_model(X.iloc[idx], y[idx], ntree=ntree, seed=seed + i, **tune))
    return mods


def rf_model_avg_predictions(mod_list: list[RandomForestRegressor], predict_df) -> np.ndarray:
    X = pd.DataFrame(predict_df).astype(float)
    cols = getattr(mod_list[0], "feature_names_", list(X.columns))
    Xv = X[cols].values
    preds = np.column_stack([m.predict(Xv) for m in mod_list])
    return preds.mean(axis=1)
