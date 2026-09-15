"""The LightGBM benchmark: features, objectives, folds and cross-validation."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd

FEATURES = ["veh_brand", "veh_gas", "region", "driv_age", "veh_age", "bonus_malus", "density", "veh_power"]
CATEGORICAL = ["veh_brand", "veh_gas", "region"]
VARIANCE_POWER = 1.5
SEED = 20260914
FOLD_SEEDS = (SEED, SEED + 1, SEED + 2)
FOLDS = 5
MAX_ROUNDS = 5000
EARLY_STOPPING_ROUNDS = 200
BASE_PARAMS = {
    "learning_rate": 0.05,
    "verbose": -1,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 8,
    "seed": SEED,
}
GRID = [{"num_leaves": leaves, "min_data_in_leaf": min_data} for leaves in (7, 15, 31, 63) for min_data in (100, 500, 2000, 5000)]
TWEEDIE = {"objective": "tweedie", "tweedie_variance_power": VARIANCE_POWER, "metric": "tweedie"}
POISSON = {"objective": "poisson", "metric": "poisson"}


def tweedie_deviance(rate, mu, weight, power: float = VARIANCE_POWER) -> float:
    rate, mu, weight = (np.asarray(x, dtype=float) for x in (rate, mu, weight))
    unit = 2 * (np.power(rate, 2 - power) / ((1 - power) * (2 - power))
                - rate * np.power(mu, 1 - power) / (1 - power)
                + np.power(mu, 2 - power) / (2 - power))
    return float(np.sum(weight * unit))


def balance_factor(rate, prediction, weight) -> float:
    return float(np.sum(np.asarray(weight) * np.asarray(rate)) / np.sum(np.asarray(weight) * np.asarray(prediction)))


def tweedie_score(rate, mu, weight, power: float = VARIANCE_POWER) -> float:
    rate, mu, weight = (np.asarray(x, dtype=float) for x in (rate, mu, weight))
    return float(np.sum(weight * np.power(mu, 1 - power) * (rate - mu)) / np.sum(weight * np.power(mu, 2 - power)))


def poisson_deviance(rate, mu, weight) -> float:
    rate, mu, weight = (np.asarray(x, dtype=float) for x in (rate, mu, weight))
    safe = np.where(rate > 0, rate, 1.0)
    unit = 2 * (np.where(rate > 0, rate * np.log(safe / mu), 0.0) - (rate - mu))
    return float(np.sum(weight * unit))


def fold_ids(frame: pd.DataFrame, by: str, seed: int = SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    if by == "risk_group":
        groups, inverse = np.unique(frame["risk_group"].to_numpy(), return_inverse=True)
        return rng.integers(0, FOLDS, groups.size)[inverse]
    return rng.integers(0, FOLDS, len(frame))


def dataset(frame: pd.DataFrame, rate, weight) -> lgb.Dataset:
    return lgb.Dataset(frame[FEATURES], label=np.asarray(rate), weight=np.asarray(weight),
                       categorical_feature=CATEGORICAL, free_raw_data=False)


def cross_validate(frame: pd.DataFrame, rate, weight, objective: dict, params: dict, folds: np.ndarray) -> dict:
    splits = [(np.flatnonzero(folds != k), np.flatnonzero(folds == k)) for k in range(FOLDS)]
    result = lgb.cv(
        {**BASE_PARAMS, **objective, **params}, dataset(frame, rate, weight), num_boost_round=MAX_ROUNDS,
        folds=splits, callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)], return_cvbooster=True,
    )
    cvbooster = result["cvbooster"]
    out_of_fold = np.empty(len(frame))
    features = frame[FEATURES]
    for (_, valid), booster in zip(splits, cvbooster.boosters):
        out_of_fold[valid] = booster.predict(features.iloc[valid], num_iteration=cvbooster.best_iteration)
    return {"rounds": cvbooster.best_iteration, "out_of_fold": out_of_fold}


def fit(frame: pd.DataFrame, rate, weight, objective: dict, params: dict, rounds: int) -> lgb.Booster:
    return lgb.train({**BASE_PARAMS, **objective, **params}, dataset(frame, rate, weight), num_boost_round=rounds)
