"""Checks for the frequency bands and for docs/frequency-banding.md.

R/frequency_banding.R takes several minutes, so it is not re-run here. These
tests check the things that can go wrong between runs, quickly:

  the bands themselves    every observed value falls in a band, and the bounds
                          are strictly increasing, read from R/bands.R
  the document is stale   the data or the chosen terms have moved since it was
                          generated
  the document disagrees  the specification it marks as chosen is not the one
    with itself           its own table says has the lowest holdout deviance

The last one guards against the easiest undetectable mistake with a generated
report: someone editing the chosen terms in R/frequency.R to a specification the
evidence did not pick, and leaving the document to vouch for it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import r_vector, scalar
from frame import canonical_md5, load_frequency_frame

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "frequency-banding.md"
BANDS_R = REPO_ROOT / "R" / "bands.R"
FREQUENCY_R = REPO_ROOT / "R" / "frequency.R"

REGENERATE = "re-run: Rscript R/frequency_banding.R"

BAND_CONSTANTS = {
    "driv_age": "DRIVER_AGE_LOWER",
    "veh_age": "VEH_AGE_LOWER",
    "bonus_malus": "BONUS_MALUS_LOWER",
}


@pytest.fixture(scope="module")
def document() -> str:
    if not DOCUMENT.exists():
        pytest.fail(f"{DOCUMENT.name} is missing; {REGENERATE}")
    return DOCUMENT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("column, constant", sorted(BAND_CONSTANTS.items()))
def test_band_bounds_are_strictly_increasing(column, constant):
    bounds = [float(b) for b in r_vector(BANDS_R, constant)]
    assert bounds == sorted(set(bounds)), f"{constant} is not strictly increasing"


@pytest.mark.parametrize("column, constant", sorted(BAND_CONSTANTS.items()))
def test_every_observed_value_has_a_band(staged, column, constant):
    """Contiguous lower bounds leave only one way to miss: a value below the first."""
    first = float(r_vector(BANDS_R, constant)[0])
    lowest = scalar(staged, f"SELECT min({column}) FROM model.frequency_frame")
    assert lowest >= first, (
        f"{column} has values down to {lowest}, below the first band at {first}"
    )


def test_bonus_malus_floor_and_entry_have_their_own_bands():
    """50 and 100 are states of the scale, and the model relies on seeing them alone."""
    bounds = [int(b) for b in r_vector(BANDS_R, "BONUS_MALUS_LOWER")]
    assert bounds[0] == 50 and bounds[1] == 51, "bonus-malus 50 must be a band on its own"
    assert 100 in bounds and 101 in bounds, "bonus-malus 100 must be a band on its own"


def test_vehicle_age_zero_is_a_band_on_its_own():
    bounds = [int(b) for b in r_vector(BANDS_R, "VEH_AGE_LOWER")]
    assert bounds[:2] == [0, 1]


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

def test_document_was_generated_from_the_current_frame(staged, document):
    if scalar(staged, "SELECT to_regclass('model.frequency_frame')") is None:
        pytest.skip("model frame not built")
    stamped = re.search(r"frame md5 `([0-9a-f]{32})`", document)
    assert stamped, "document carries no frame md5"
    current = canonical_md5(load_frequency_frame(staged))
    assert stamped.group(1) == current, (
        f"the document describes frame {stamped.group(1)}, the view now serves "
        f"{current}; {REGENERATE}"
    )


def test_the_model_uses_the_terms_the_document_chose(document):
    chosen = re.search(r"^Chosen terms: `(.+)`$", document, re.M)
    assert chosen, "document names no chosen terms"
    model_terms = " + ".join(r_vector(FREQUENCY_R, "FREQUENCY_TERMS"))
    assert chosen.group(1) == model_terms, (
        f"R/frequency.R fits {model_terms!r} but the banding analysis chose "
        f"{chosen.group(1)!r}; either the model was changed without evidence or "
        f"the document is stale, {REGENERATE}"
    )


def test_the_chosen_specification_has_the_lowest_holdout_deviance(document):
    rows = re.findall(r"^\| (\*\*)?([^|*]+?)(\*\*)? \|((?: [^|]* \|){6})$", document, re.M)
    table = []
    for bold_open, label, _bold_close, cells in rows:
        values = [c.strip() for c in cells.strip(" |").split("|")]
        if len(values) != 6:
            continue
        try:
            holdout = float(values[4].replace(",", ""))
        except ValueError:
            continue
        table.append((label.strip(), bool(bold_open), holdout))

    assert len(table) >= 2, "could not read the specification table"
    marked = [label for label, bold, _ in table if bold]
    assert len(marked) == 1, f"expected exactly one chosen specification, got {marked}"
    lowest = min(table, key=lambda r: r[2])[0]
    assert marked[0] == lowest, (
        f"the document marks {marked[0]!r} as chosen, but {lowest!r} has the lowest "
        f"holdout deviance in its own table"
    )
