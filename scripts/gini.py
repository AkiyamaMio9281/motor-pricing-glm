"""Gini indices for a pricing model, each named by what it orders, weights and accumulates."""

from __future__ import annotations

import numpy as np
from scipy.stats import rankdata


def lorenz_curve(predicted, actual, exposure) -> tuple[np.ndarray, np.ndarray]:
    predicted, actual, exposure = (np.asarray(x, dtype=float) for x in (predicted, actual, exposure))
    _, tie = np.unique(predicted, return_inverse=True)
    x = np.r_[0.0, np.cumsum(np.bincount(tie, weights=exposure))]
    y = np.r_[0.0, np.cumsum(np.bincount(tie, weights=actual))]
    return x / x[-1], y / y[-1]


def lorenz_gini(predicted, actual, exposure) -> float:
    x, y = lorenz_curve(predicted, actual, exposure)
    return float(1 - 2 * np.trapezoid(y, x))


def lorenz_gini_ties_in_row_order(predicted, actual, exposure) -> float:
    predicted, actual, exposure = (np.asarray(x, dtype=float) for x in (predicted, actual, exposure))
    order = np.lexsort((np.arange(predicted.size), predicted))
    x = np.r_[0.0, np.cumsum(exposure[order])]
    y = np.r_[0.0, np.cumsum(actual[order])]
    return float(1 - 2 * np.trapezoid(y / y[-1], x / x[-1]))


def row_gini(predicted, actual) -> float:
    predicted, actual = np.asarray(predicted, dtype=float), np.asarray(actual, dtype=float)
    n = predicted.size
    order = np.lexsort((np.arange(n), -predicted))
    return float(np.cumsum(actual[order]).sum() / actual.sum() / n - (n + 1) / (2 * n))


def normalized_row_gini(predicted, actual) -> float:
    return row_gini(predicted, actual) / row_gini(actual, actual)


def auc_gini(score, event) -> float:
    score, event = np.asarray(score, dtype=float), np.asarray(event, dtype=bool)
    ranks = rankdata(score)
    positives, negatives = event.sum(), (~event).sum()
    auc = (ranks[event].sum() - positives * (positives + 1) / 2) / (positives * negatives)
    return float(2 * auc - 1)
