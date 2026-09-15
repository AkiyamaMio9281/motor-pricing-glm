from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import benchmark as bm
import pure_premium as pp
from conftest import scalar
from frame import canonical_md5

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "lightgbm-baseline.md"
REGENERATE = "re-run: .venv/Scripts/python scripts/lightgbm_baseline.py"

RUN = pp.BenchmarkRun("synthetic", "0" * 32, "all", 1000.0, 1.5, 1.5, 100, 1.0)


@pytest.fixture
def consistent():
    return pd.DataFrame({
        "exposure": [0.5, 1.0, 0.25, 1.0],
        "capped_amount_per_year": [200.0, 200.0, 400.0, 200.0],
        "capped_loss": [100.0, 200.0, 100.0, 200.0],
        "incurred_loss": [150.0, 300.0, 150.0, 300.0],
        "trained": [True, True, True, True],
    })


def test_the_variance_power_follows_from_the_capped_gamma_shape():
    diagnostics = (REPO_ROOT / "docs" / "model-diagnostics.md").read_text(encoding="utf-8")
    shape = re.search(r"and ([\d.]+) capped", diagnostics)
    assert shape, "model-diagnostics.md states no capped Gamma shape"
    alpha = float(shape.group(1))
    assert abs((alpha + 2) / (alpha + 1) - bm.VARIANCE_POWER) < 0.01, "VARIANCE_POWER no longer follows from the capped Gamma shape"


def test_tweedie_deviance_is_zero_at_the_data_and_positive_elsewhere():
    rate = np.array([0.0, 50.0, 400.0])
    weight = np.array([0.5, 1.0, 0.25])
    assert bm.tweedie_deviance(rate[1:], rate[1:], weight[1:]) == pytest.approx(0, abs=1e-9)
    assert bm.tweedie_deviance(rate, rate + 10, weight) > 0
    p = bm.VARIANCE_POWER
    assert bm.tweedie_deviance([0.0], [100.0], [2.0]) == pytest.approx(2 * 2 * 100 ** (2 - p) / (2 - p))


def test_risk_group_folds_never_split_a_group_and_row_folds_do():
    frame = pd.DataFrame({"risk_group": np.repeat(np.arange(2000), 3)})
    by_group = bm.fold_ids(frame, "risk_group")
    by_row = bm.fold_ids(frame, "row")
    assert (pd.Series(by_group).groupby(frame["risk_group"]).nunique() == 1).all(), "a risk group is split across folds"
    assert (pd.Series(by_row).groupby(frame["risk_group"]).nunique() > 1).any()
    assert set(by_group) == set(range(bm.FOLDS))


def test_a_balanced_benchmark_assembles(consistent):
    result = pp.assemble_benchmark(RUN, consistent)
    assert result.off_balance == pytest.approx(1)
    assert result.frame["expected_loss"].sum() == pytest.approx(consistent["incurred_loss"].sum())


def test_an_unbalanced_benchmark_is_refused_until_corrected(consistent):
    consistent["capped_amount_per_year"] *= 0.9566
    with pytest.raises(pp.BasisError, match="capped loss per policy-year"):
        pp.assemble_benchmark(RUN, consistent)
    factor = bm.balance_factor(consistent["capped_loss"] / consistent["exposure"], consistent["capped_amount_per_year"],
                               consistent["exposure"])
    consistent["capped_amount_per_year"] *= factor
    assert pp.assemble_benchmark(RUN, consistent).off_balance == pytest.approx(1)


def test_a_benchmark_carrying_the_load_twice_is_refused(consistent):
    consistent["capped_amount_per_year"] *= RUN.large_loss_load
    with pytest.raises(pp.BasisError, match="without the large-loss load"):
        pp.assemble_benchmark(RUN, consistent)


@pytest.fixture(scope="module")
def stored(staged):
    if scalar(staged, "SELECT to_regclass('model.benchmark_run')") is None:
        pytest.skip("model.benchmark_run not built; run scripts/migrate.py")
    if scalar(staged, f"SELECT count(*) FROM model.benchmark_run WHERE run = '{pp.BENCHMARK_RUN}'") == 0:
        pytest.skip(f"no benchmark predictions; {REGENERATE}")
    return staged


@pytest.fixture(scope="module")
def benchmark(stored):
    return pp.benchmark_pure_premium(stored, pp.BENCHMARK_RUN)


@pytest.fixture(scope="module")
def document() -> str:
    if not DOCUMENT.exists():
        pytest.fail(f"{DOCUMENT.name} is missing; {REGENERATE}")
    return DOCUMENT.read_text(encoding="utf-8")


def test_the_benchmark_is_trained_on_the_validation_runs_rows(benchmark, stored):
    assert benchmark.run.trained_on == scalar(stored, f"SELECT trained_on FROM model.glm_run WHERE run = '{pp.VALIDATION_RUN}'")
    assert benchmark.run.large_loss_load == scalar(stored, f"SELECT large_loss_load FROM model.glm_run WHERE run = '{pp.VALIDATION_RUN}'")
    assert (benchmark.frame["trained"] == ~benchmark.frame["risk_group_holdout"]).all()


def test_stored_predictions_balance_to_capped_losses_on_training_rows(benchmark):
    trained = benchmark.frame[benchmark.frame["trained"]]
    assert pp.check_capped_amount_per_year(trained) == pytest.approx(1, abs=1e-9)
    assert abs(benchmark.off_balance - 1) <= pp.LOSS_BALANCE_TOLERANCE


def test_document_matches_the_stored_run(document, benchmark, stored):
    stamped = re.search(r"Frame md5 `([0-9a-f]{32})`", document)
    assert stamped and stamped.group(1) == canonical_md5(benchmark.frame), REGENERATE
    parameters = scalar(stored, f"SELECT parameters FROM model.benchmark_run WHERE run = '{pp.BENCHMARK_RUN}'")
    parameters = parameters if isinstance(parameters, dict) else json.loads(parameters)
    chosen = (f"Chosen: {parameters['num_leaves']} leaves, a minimum leaf of {parameters['min_data_in_leaf']:,} rows, "
              f"{benchmark.run.boosting_rounds} rounds.")
    assert chosen in document, REGENERATE
    assert f"balance correction of {benchmark.run.balance_factor:.4f}" in document, REGENERATE
