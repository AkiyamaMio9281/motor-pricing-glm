"""Paired bootstrap over held-out risk groups for the gaps between LightGBM and the GLM.

    .venv/Scripts/python scripts/bootstrap_gap.py
"""

from __future__ import annotations

import numpy as np
import psycopg

import benchmark as bm
import gini as g
import pure_premium as pp
from db import dsn

RESAMPLES = 1000
SEED = 20260915


def group_weights(group, rng: np.random.Generator) -> np.ndarray:
    _, index = np.unique(np.asarray(group), return_inverse=True)
    groups = index.max() + 1
    return np.bincount(rng.integers(0, groups, groups), minlength=groups)[index].astype(float)


def paired_bootstrap(statistic, group, resamples: int = RESAMPLES, seed: int = SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.array([statistic(group_weights(group, rng)) for _ in range(resamples)])


def summarise(observed: float, draws: np.ndarray) -> dict:
    low, high = np.percentile(draws, [2.5, 97.5])
    return {"observed": float(observed), "low": float(low), "high": float(high), "favours_lightgbm": float((draws > 0).mean())}


def main() -> None:
    with psycopg.connect(dsn()) as conn:
        policies = pp.load_policies(conn)
        glm = pp.pure_premium(conn, pp.VALIDATION_RUN, policies).frame
        gbm = pp.benchmark_pure_premium(conn, pp.BENCHMARK_RUN, policies).frame

    held = glm["risk_group_holdout"].to_numpy()
    exposure = glm.loc[held, "exposure"].to_numpy()
    capped = glm.loc[held, "capped_loss"].to_numpy()
    recorded = glm.loc[held, "incurred_loss"].to_numpy()
    group = glm.loc[held, "risk_group"].to_numpy()
    premium = {"GLM": glm.loc[held, "amount_per_year"].to_numpy(), "LightGBM": gbm.loc[held, "amount_per_year"].to_numpy()}
    capped_rate = {"GLM": (glm["claims_per_year"] * glm["capped_amount_per_claim"]).to_numpy()[held],
                   "LightGBM": gbm.loc[held, "capped_amount_per_year"].to_numpy()}
    deviance = {m: exposure * bm.tweedie_unit_deviance(capped / exposure, capped_rate[m]) for m in premium}
    glm_deviance_total = deviance["GLM"].sum()

    statistics = {
        "Gini gap, capped losses": lambda w: g.lorenz_gini(premium["LightGBM"], capped * w, exposure * w)
        - g.lorenz_gini(premium["GLM"], capped * w, exposure * w),
        "Gini gap, recorded losses": lambda w: g.lorenz_gini(premium["LightGBM"], recorded * w, exposure * w)
        - g.lorenz_gini(premium["GLM"], recorded * w, exposure * w),
        "capped Tweedie deviance, GLM minus LightGBM, share of GLM": lambda w: np.sum(w * (deviance["GLM"] - deviance["LightGBM"]))
        / np.sum(w * deviance["GLM"]),
    }
    print(f"{held.sum():,} held-out policy-years in {np.unique(group).size:,} risk groups; {RESAMPLES} resamples, seed {SEED}")
    print(f"GLM capped Tweedie deviance {glm_deviance_total:,.0f}")
    print("| Gap, positive favours LightGBM | Observed | 95% interval | Resamples favouring LightGBM |")
    print("|---|---|---|---|")
    for name, statistic in statistics.items():
        result = summarise(statistic(np.ones(held.sum())), paired_bootstrap(statistic, group))
        print(f"| {name} | {result['observed']:.4f} | {result['low']:.4f} to {result['high']:.4f} | "
              f"{100 * result['favours_lightgbm']:.1f}% |")


if __name__ == "__main__":
    main()
