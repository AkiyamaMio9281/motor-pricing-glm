from __future__ import annotations

import numpy as np

import bootstrap_gap as bg
import gini as g


def test_a_resample_keeps_every_risk_group_whole():
    group = np.repeat(np.arange(300), 3)
    weights = bg.group_weights(group, np.random.default_rng(0))
    per_group = weights.reshape(300, 3)
    assert (per_group == per_group[:, :1]).all(), "rows of one risk group were resampled separately"
    assert per_group[:, 0].sum() == 300, "the resample does not draw as many groups as it holds"


def test_identical_models_have_no_gap_in_any_resample():
    rng = np.random.default_rng(1)
    premium = rng.gamma(2.0, 50.0, 900)
    actual = np.where(rng.uniform(size=900) < 0.1, premium * 5, 0.0)
    exposure = rng.uniform(0.1, 1.0, 900)
    group = np.repeat(np.arange(300), 3)

    def gap(w):
        return g.lorenz_gini(premium, actual * w, exposure * w) - g.lorenz_gini(premium.copy(), actual * w, exposure * w)

    assert np.all(bg.paired_bootstrap(gap, group, resamples=50) == 0)


def test_a_model_that_knows_the_outcome_wins_every_resample():
    rng = np.random.default_rng(2)
    exposure = rng.uniform(0.1, 1.0, 3000)
    risk = rng.gamma(2.0, 50.0, 3000)
    actual = rng.poisson(risk * exposure / 100) * 100.0
    noise = risk * np.exp(rng.normal(0, 1.5, 3000))
    group = np.arange(3000)

    def gap(w):
        return g.lorenz_gini(risk, actual * w, exposure * w) - g.lorenz_gini(noise, actual * w, exposure * w)

    summary = bg.summarise(gap(np.ones(3000)), bg.paired_bootstrap(gap, group, resamples=200))
    assert summary["low"] > 0 and summary["favours_lightgbm"] == 1.0
