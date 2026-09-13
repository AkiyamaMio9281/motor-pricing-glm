"""Checks for docs/frequency-dispersion.md.

The generating script refuses to write the document unless the claims it argues
from hold. These tests cover what can change afterwards without re-running it:

  staleness      the frame or the model's terms have moved since generation
  consistency    the tables still say what the conclusion rests on: every observed
                 statistic outside its simulated range, and clustered sandwich
                 errors smaller than the quasi-Poisson rescaling
  the function   the conclusion names frequency_vcov() and risk_groups(), so both
                 must exist in the R source it points to
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import r_vector, scalar
from frame import canonical_md5, load_frequency_frame

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "frequency-dispersion.md"
FREQUENCY_R = REPO_ROOT / "R" / "frequency.R"
MODEL_FRAME_R = REPO_ROOT / "R" / "model_frame.R"

REGENERATE = "re-run: Rscript R/frequency_dispersion.R"


@pytest.fixture(scope="module")
def document() -> str:
    if not DOCUMENT.exists():
        pytest.fail(f"{DOCUMENT.name} is missing; {REGENERATE}")
    return DOCUMENT.read_text(encoding="utf-8")


def table_after(document: str, heading: str) -> list[list[str]]:
    """Rows of the first markdown table after a heading, header and rule excluded."""
    section = document.split(heading, 1)
    assert len(section) == 2, f"no section {heading!r}"
    rows = []
    for line in section[1].splitlines():
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif rows:
            break
    return rows[2:]


def number(cell: str) -> float:
    return float(cell.replace(",", ""))


def test_document_was_generated_from_the_current_frame(staged, document):
    if scalar(staged, "SELECT to_regclass('model.frequency_frame')") is None:
        pytest.skip("model frame not built")
    stamped = re.search(r"frame md5 `([0-9a-f]{32})`", document)
    assert stamped, "document carries no frame md5"
    current = canonical_md5(load_frequency_frame(staged))
    assert stamped.group(1) == current, f"stale: {REGENERATE}"


def test_document_describes_the_pricing_model(document):
    terms = " + ".join(r_vector(FREQUENCY_R, "FREQUENCY_TERMS"))
    assert f"`{terms}`" in document, f"R/frequency.R now fits different terms; {REGENERATE}"


def test_every_statistic_lies_outside_its_simulated_range(document):
    rows = table_after(document, "## Three statistics")
    assert len(rows) == 3
    for statistic, observed, _mean, simulated_range, _one, verdict in rows:
        low, high = (number(x) for x in simulated_range.split(" to "))
        value = number(observed)
        assert value > high or value < low, f"{statistic}: {value} inside [{low}, {high}]"
        expected = "above every simulation" if value > high else "below every simulation"
        assert verdict == expected, f"{statistic}: says {verdict!r}, numbers say {expected!r}"


def test_clustered_errors_are_smaller_than_the_quasi_poisson_rescaling(document):
    rows = {r[0]: r for r in table_after(document, "## What it does to standard errors")}
    quasi = number(rows["quasi-Poisson, scaled by the Pearson dispersion"][1])
    clustered = number(rows["sandwich, clustered by risk group"][1])
    assert clustered < quasi, (
        f"clustered median {clustered} is not below quasi-Poisson {quasi}; the "
        "conclusion against quasi-Poisson no longer holds"
    )


def test_the_functions_the_conclusion_names_exist(document):
    assert "frequency_vcov(fit, cluster = risk_groups(frame))" in document
    assert re.search(r"^frequency_vcov <- function\(", FREQUENCY_R.read_text(encoding="utf-8"), re.M)
    assert re.search(r"^risk_groups <- function\(", MODEL_FRAME_R.read_text(encoding="utf-8"), re.M)
