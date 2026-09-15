from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np
import pytest
from sklearn.metrics import roc_auc_score

import gini as g
import pure_premium as pp
from conftest import scalar
from frame import canonical_md5

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "gini.md"
FIGURE = REPO_ROOT / "docs" / "figures" / "lorenz-curves.png"
REGENERATE = "re-run: .venv/Scripts/python scripts/gini_report.py"


@pytest.fixture
def portfolio():
    rng = np.random.default_rng(7)
    exposure = rng.uniform(0.1, 1.0, 2000)
    rate = rng.gamma(2.0, 50.0, 2000)
    actual = np.where(rng.uniform(size=2000) < 0.1, rate * exposure * 10, 0.0)
    return rate, actual, exposure


def test_a_constant_premium_scores_zero(portfolio):
    _, actual, exposure = portfolio
    assert g.lorenz_gini(np.full(actual.size, 3.0), actual, exposure) == pytest.approx(0, abs=1e-12)


def test_the_curve_runs_from_origin_to_one(portfolio):
    x, y = g.lorenz_curve(*portfolio)
    assert (x[0], y[0], x[-1], y[-1]) == (0.0, 0.0, 1.0, 1.0)
    assert (np.diff(x) > 0).all()


def test_merged_ties_make_the_gini_independent_of_row_order(portfolio):
    rate, actual, exposure = portfolio
    banded = np.round(rate, -2)
    shuffled = np.random.default_rng(1).permutation(rate.size)
    assert g.lorenz_gini(banded, actual, exposure) == pytest.approx(
        g.lorenz_gini(banded[shuffled], actual[shuffled], exposure[shuffled]), abs=1e-12), "tied predictions are not merged"


def test_sorting_the_other_way_negates_it(portfolio):
    rate, actual, exposure = portfolio
    assert g.lorenz_gini(-rate, actual, exposure) == pytest.approx(-g.lorenz_gini(rate, actual, exposure), abs=1e-12)


def test_a_premium_that_knows_the_outcome_beats_one_that_does_not(portfolio):
    rate, actual, exposure = portfolio
    assert g.lorenz_gini(actual / exposure, actual, exposure) > g.lorenz_gini(rate, actual, exposure) > 0, "the Gini is not sorted lowest premium first"


def test_auc_gini_agrees_with_scikit_learn(portfolio):
    rate, actual, _ = portfolio
    assert g.auc_gini(rate, actual > 0) == pytest.approx(2 * roc_auc_score(actual > 0, rate) - 1, abs=1e-12), "auc_gini disagrees with 2 x AUC - 1"


def test_the_normalized_row_gini_of_the_outcome_itself_is_one(portfolio):
    _, actual, _ = portfolio
    assert g.normalized_row_gini(actual, actual) == pytest.approx(1)


@pytest.fixture(scope="module")
def document() -> str:
    if not DOCUMENT.exists():
        pytest.fail(f"{DOCUMENT.name} is missing; {REGENERATE}")
    return DOCUMENT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def holdout(staged):
    if scalar(staged, f"SELECT count(*) FROM model.benchmark_run WHERE run = '{pp.BENCHMARK_RUN}'") == 0:
        pytest.skip("no benchmark predictions; run scripts/lightgbm_baseline.py")
    policies = pp.load_policies(staged)
    glm = pp.pure_premium(staged, pp.VALIDATION_RUN, policies)
    gbm = pp.benchmark_pure_premium(staged, pp.BENCHMARK_RUN, policies)
    return glm.frame, gbm.frame


def test_document_reports_the_gini_of_the_current_predictions(document, holdout):
    glm, gbm = holdout
    stamped = re.search(r"Frame md5 `([0-9a-f]{32})`", document)
    assert stamped and stamped.group(1) == canonical_md5(glm), REGENERATE
    held = glm["risk_group_holdout"].to_numpy()
    for model, frame in (("GLM", glm), ("LightGBM", gbm)):
        value = g.lorenz_gini(frame.loc[held, "amount_per_year"], frame.loc[held, "capped_loss"], frame.loc[held, "exposure"])
        row = re.search(rf"^\| {model} \| ([\d.]+) \|", document, re.M)
        assert row and row.group(1) == f"{value:.3f}", REGENERATE


def test_the_definition_sits_next_to_the_number_in_results():
    results = (REPO_ROOT / "RESULTS.md").read_text(encoding="utf-8")
    section = re.search(r"^### Gini\n(.*?)(?=^### |^## )", results, re.M | re.S)
    assert section, "RESULTS.md has no Gini section"
    text = section.group(1)
    assert "**Definition.**" in text and "ordered Lorenz" in text, "RESULTS.md reports a Gini without its definition"
    assert text.index("**Definition.**") < text.index("| GLM |"), "the definition must come before the numbers"


def test_the_figure_is_referenced_committed_and_a_png(document):
    assert "](figures/lorenz-curves.png)" in document, "gini.md does not embed the Lorenz figure"
    assert FIGURE.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and FIGURE.stat().st_size > 5000
    ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", str(FIGURE.relative_to(REPO_ROOT))], cwd=REPO_ROOT)
    assert ignored.returncode != 0, "docs/figures/lorenz-curves.png is ignored by .gitignore"
