"""Tests for model.frequency_frame and the two loaders that read it.

The claim this layer makes is that R and Python model the same data. The
strongest form of that claim is checkable, so it is checked: both languages
render every row of the frame as canonical text and the md5s must be equal. A
second test proves that check can fail, by moving one exposure value by the
smallest representable amount and watching the hash change.
"""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from conftest import scalar
from frame import (
    CATEGORICAL_COLUMNS,
    canonical_md5,
    fingerprint,
    load_frequency_frame,
)
from r_runtime import find_rscript

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def framed(staged):
    if scalar(staged, "SELECT to_regclass('model.frequency_frame')") is None:
        pytest.skip("model frame not built; run scripts/migrate.py")
    return staged


@pytest.fixture(scope="module")
def frame(framed):
    return load_frequency_frame(framed)


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

def test_view_has_one_row_per_policy_year(framed):
    assert scalar(framed, "SELECT count(*) FROM model.frequency_frame") == scalar(
        framed, "SELECT count(*) FROM fact.exposure"
    )
    assert scalar(
        framed, "SELECT count(DISTINCT idpol) FROM model.frequency_frame"
    ) == 678_013


def test_view_exposes_codes_and_never_surrogate_keys(framed):
    with framed.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'model' AND table_name = 'frequency_frame'"
        )
        columns = {row[0] for row in cur.fetchall()}
    assert not {c for c in columns if c.endswith("_key")}
    assert {"area", "region", "veh_brand", "veh_gas"} <= columns


def test_view_does_not_offer_the_uncapped_exposure(framed):
    """One exposure column, so offset(log(exposure_raw)) cannot be typed by accident."""
    with framed.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'model' AND table_name = 'frequency_frame' "
            "AND column_name LIKE 'exposure%'"
        )
        assert [row[0] for row in cur.fetchall()] == ["exposure"]


def test_view_totals_match_the_fact_table(framed):
    assert scalar(framed, "SELECT sum(claim_nb) FROM model.frequency_frame") == 36_102
    assert abs(
        scalar(framed, "SELECT sum(exposure) FROM model.frequency_frame")
        - float(scalar(framed, "SELECT sum(exposure) FROM fact.exposure"))
    ) < 1e-6


def test_the_fact_table_would_hand_python_decimals(framed):
    """The reason the view casts exposure: without it, pandas gets object dtype."""
    with framed.cursor() as cur:
        cur.execute("SELECT exposure FROM fact.exposure LIMIT 1")
        assert isinstance(cur.fetchone()[0], Decimal)
        cur.execute("SELECT exposure FROM model.frequency_frame LIMIT 1")
        assert isinstance(cur.fetchone()[0], float)


# ---------------------------------------------------------------------------
# The Python loader
# ---------------------------------------------------------------------------

def test_python_frame_types(frame):
    assert frame["exposure"].dtype == "float64"
    assert frame["claim_nb"].dtype == "int64"
    for column in CATEGORICAL_COLUMNS:
        assert isinstance(frame[column].dtype, pd.CategoricalDtype), column
    assert not (frame.dtypes == object).any(), frame.dtypes[frame.dtypes == object]


def test_python_frame_levels_come_from_the_dimensions(frame):
    assert len(frame["region"].cat.categories) == 22
    assert len(frame["area"].cat.categories) == 6
    assert len(frame["veh_gas"].cat.categories) == 2
    brands = list(frame["veh_brand"].cat.categories)
    assert len(brands) == 11
    assert brands.index("B2") < brands.index("B10"), "levels must be in natural order"


def test_python_frame_is_sorted_by_policy(frame):
    assert frame["idpol"].is_monotonic_increasing


# ---------------------------------------------------------------------------
# The cross-language check
# ---------------------------------------------------------------------------

def test_the_md5_detects_a_one_ulp_change_in_one_exposure(frame):
    """Prove the canonical rendering is fine enough to be worth comparing.

    %.17g identifies a double uniquely, so moving a single value to the next
    representable double must change the hash. If it did not, equal hashes
    across languages would prove much less than they appear to.
    """
    original = canonical_md5(frame)
    nudged = frame.copy()
    row = nudged.index[1000]
    nudged.loc[row, "exposure"] = np.nextafter(nudged.loc[row, "exposure"], 2.0)
    assert nudged.loc[row, "exposure"] != frame.loc[row, "exposure"]
    assert canonical_md5(nudged) != original


def test_r_and_python_receive_byte_identical_frames(frame):
    rscript = find_rscript()
    if rscript is None:
        pytest.skip("Rscript not found; set RSCRIPT or install R to run this check")

    completed = subprocess.run(
        [str(rscript), "R/check_frame.R"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert completed.returncode == 0, completed.stderr

    json_line = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    assert json_line, f"no fingerprint in R output:\n{completed.stdout}\n{completed.stderr}"
    from_r = json.loads(json_line[-1])
    from_python = fingerprint(frame)

    for key in ("rows", "claims", "short_exposure_rows", "levels"):
        assert from_r[key] == from_python[key], key
    assert abs(from_r["exposure_sum"] - from_python["exposure_sum"]) < 1e-6
    assert from_r["md5"] == from_python["md5"], (
        "R and Python read different frames from the same view"
    )
