from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

import calibration as cal
import pure_premium as pp
from conftest import scalar
from frame import canonical_md5

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "calibration.md"
FIGURES = ("decile-lift.png", "calibration-by-segment.png")
REGENERATE = "re-run: .venv/Scripts/python scripts/calibration_report.py"


def test_deciles_hold_equal_exposure_in_prediction_order():
    rng = np.random.default_rng(3)
    predicted = rng.gamma(2.0, 50.0, 5000)
    exposure = rng.uniform(0.05, 1.0, 5000)
    decile = cal.equal_exposure_deciles(predicted, exposure)
    shares = np.bincount(decile, weights=exposure) / exposure.sum()
    assert np.allclose(shares, 0.1, atol=0.002), "deciles do not hold equal exposure"
    order = np.argsort(predicted)
    assert (np.diff(decile[order]) >= 0).all(), "a higher prediction fell in a lower decile"


def test_identical_predictions_share_a_decile():
    predicted = np.repeat([1.0, 2.0, 3.0], [500, 1000, 500])
    decile = cal.equal_exposure_deciles(predicted, np.ones(predicted.size))
    for value in (1.0, 2.0, 3.0):
        assert np.unique(decile[predicted == value]).size == 1, "tied predictions were split across deciles"


def test_ratio_interval_widens_when_rows_move_together():
    rng = np.random.default_rng(5)
    expected = rng.uniform(50, 150, 400)
    actual = expected * rng.gamma(1.0, 1.0, 400)
    ratio, low, high = cal.ratio_with_interval(actual, expected, np.arange(400))
    assert ratio == pytest.approx(actual.sum() / expected.sum())
    assert low < ratio < high
    doubled = cal.ratio_with_interval(np.repeat(actual, 2), np.repeat(expected, 2), np.repeat(np.arange(400), 2))
    independent = cal.ratio_with_interval(np.repeat(actual, 2), np.repeat(expected, 2), np.arange(800))
    assert (doubled[2] - doubled[1]) == pytest.approx(np.sqrt(2) * (independent[2] - independent[1])), (
        "duplicated rows in one risk group should widen the interval by the square root of 2"
    )


@pytest.fixture(scope="module")
def document() -> str:
    if not DOCUMENT.exists():
        pytest.fail(f"{DOCUMENT.name} is missing; {REGENERATE}")
    return DOCUMENT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def glm(staged):
    if scalar(staged, f"SELECT count(*) FROM model.glm_run WHERE run = '{pp.VALIDATION_RUN}'") == 0:
        pytest.skip("no GLM runs; run Rscript R/export_predictions.R")
    return pp.pure_premium(staged, pp.VALIDATION_RUN).frame


def test_document_matches_the_current_holdout(document, glm):
    stamped = re.search(r"Frame md5 `([0-9a-f]{32})`", document)
    assert stamped and stamped.group(1) == canonical_md5(glm), REGENERATE
    held = glm[glm["risk_group_holdout"]]
    rate = (held["claims_per_year"] * held["capped_amount_per_claim"]).to_numpy()
    exposure = held["exposure"].to_numpy()
    top = cal.equal_exposure_deciles(rate, exposure) == 9
    actual = held["capped_loss"].to_numpy()[top].sum() / exposure[top].sum()
    row = re.search(r"^\| 10 \| [\d.,]+ \| ([\d.,]+) \[", document, re.M)
    assert row and row.group(1) == f"{actual:,.1f}", REGENERATE
    short = (exposure < 0.1)
    ratio, _, _ = cal.ratio_with_interval(held["capped_loss"].to_numpy()[short], (exposure * rate)[short],
                                          held["risk_group"].to_numpy()[short])
    assert f"| exposure, years | under 0.1 | " in document and f"| {ratio:.3f} [" in document, REGENERATE


def test_results_carries_both_tables():
    results = (REPO_ROOT / "RESULTS.md").read_text(encoding="utf-8")
    assert "### Decile lift" in results and "### Calibration by segment" in results, "RESULTS.md is missing a D3-5 table"


def test_the_figures_are_referenced_committed_and_pngs(document):
    for name in FIGURES:
        path = REPO_ROOT / "docs" / "figures" / name
        assert f"](figures/{name})" in document, f"calibration.md does not embed {name}"
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and path.stat().st_size > 5000, f"{name} is not a PNG"
        ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", f"docs/figures/{name}"], cwd=REPO_ROOT)
        assert ignored.returncode != 0, f"docs/figures/{name} is ignored by .gitignore"
