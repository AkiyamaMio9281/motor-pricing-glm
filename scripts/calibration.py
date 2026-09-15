"""Equal-exposure deciles and actual-over-expected ratios with risk-group intervals."""

from __future__ import annotations

import numpy as np

Z95 = 1.959963984540054


def equal_exposure_deciles(predicted, exposure, bins: int = 10) -> np.ndarray:
    predicted, exposure = np.asarray(predicted, dtype=float), np.asarray(exposure, dtype=float)
    _, tie = np.unique(predicted, return_inverse=True)
    tie_exposure = np.bincount(tie, weights=exposure)
    midpoint = (np.cumsum(tie_exposure) - tie_exposure / 2) / tie_exposure.sum()
    return np.minimum((midpoint * bins).astype(int), bins - 1)[tie]


def ratio_with_interval(actual, expected, cluster) -> tuple[float, float, float]:
    actual, expected = np.asarray(actual, dtype=float), np.asarray(expected, dtype=float)
    ratio = actual.sum() / expected.sum()
    _, group = np.unique(np.asarray(cluster), return_inverse=True)
    residual = np.bincount(group, weights=actual - ratio * expected)
    half_width = Z95 * np.sqrt(np.sum(residual**2)) / expected.sum()
    return float(ratio), float(ratio - half_width), float(ratio + half_width)
