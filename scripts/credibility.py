"""Limited-fluctuation and Buhlmann-Straub credibility."""

from __future__ import annotations

import numpy as np
from scipy.stats import norm


def full_credibility_standard(tolerance: float = 0.05, probability: float = 0.90, severity_cv: float = 0.0) -> float:
    z = norm.ppf((1 + probability) / 2)
    return float((z / tolerance) ** 2 * (1 + severity_cv**2))


def limited_fluctuation_credibility(claims, standard: float) -> np.ndarray:
    return np.minimum(1.0, np.sqrt(np.asarray(claims, dtype=float) / standard))


def buhlmann_straub(x, weight, cell) -> dict:
    x, weight = np.asarray(x, dtype=float), np.asarray(weight, dtype=float)
    _, index = np.unique(np.asarray(cell), return_inverse=True)
    cells = index.max() + 1
    cell_weight = np.bincount(index, weights=weight, minlength=cells)
    cell_mean = np.bincount(index, weights=weight * x, minlength=cells) / cell_weight
    count = np.bincount(index, minlength=cells)
    total = cell_weight.sum()
    grand_mean = np.sum(cell_weight * cell_mean) / total
    epv = np.sum(weight * (x - cell_mean[index]) ** 2) / np.sum(count - 1)
    vhm = (np.sum(cell_weight * (cell_mean - grand_mean) ** 2) - (cells - 1) * epv) / (total - np.sum(cell_weight**2) / total)
    k = epv / vhm if vhm > 0 else np.inf
    return {"epv": float(epv), "vhm": float(vhm), "k": float(k), "grand_mean": float(grand_mean)}


def buhlmann_credibility(cell_weight, k: float) -> np.ndarray:
    cell_weight = np.asarray(cell_weight, dtype=float)
    return np.zeros_like(cell_weight) if np.isinf(k) else cell_weight / (cell_weight + k)
