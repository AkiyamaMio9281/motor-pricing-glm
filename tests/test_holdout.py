from __future__ import annotations

import hashlib
import re
from pathlib import Path

import numpy as np
import pytest

import pure_premium as pp
from conftest import scalar
from frame import canonical_md5

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "holdout-split.md"
REGENERATE = "re-run: .venv/Scripts/python scripts/holdout_report.py"
PROFILE = ["area", "veh_power", "veh_age", "driv_age", "bonus_malus", "veh_brand", "veh_gas", "density", "region"]


@pytest.fixture(scope="module")
def holdout(staged):
    if scalar(staged, "SELECT to_regclass('model.holdout')") is None:
        pytest.skip("model.holdout not built; run scripts/migrate.py")
    return staged


@pytest.fixture(scope="module")
def policies(holdout):
    return pp.load_policies(holdout)


@pytest.fixture(scope="module")
def runs(holdout):
    if scalar(holdout, f"SELECT count(*) FROM model.glm_run WHERE run = '{pp.VALIDATION_RUN}'") == 0:
        pytest.skip("no split GLM runs; run Rscript R/export_predictions.R")
    return holdout


@pytest.fixture(scope="module")
def document() -> str:
    if not DOCUMENT.exists():
        pytest.fail(f"{DOCUMENT.name} is missing; {REGENERATE}")
    return DOCUMENT.read_text(encoding="utf-8")


def test_idpol_is_unique_so_an_idpol_split_is_a_row_split(holdout):
    assert scalar(holdout, "SELECT count(DISTINCT idpol) FROM model.frequency_frame") == scalar(
        holdout, "SELECT count(*) FROM model.holdout"
    ) == scalar(holdout, "SELECT count(*) FROM model.frequency_frame")


def test_a_risk_group_is_a_run_of_identical_rating_factors(policies):
    profile = policies.groupby(PROFILE, observed=True, sort=False).ngroup().to_numpy()
    runs = np.cumsum(np.r_[True, profile[1:] != profile[:-1]])
    assert (policies["risk_group"].to_numpy() == runs).all(), "model.holdout's risk groups are not runs of identical rating factors"


def test_the_risk_group_split_is_the_holdout_d2_3_used(holdout):
    banding = (REPO_ROOT / "docs" / "frequency-banding.md").read_text(encoding="utf-8")
    documented = re.search(r"^Holdout: ([\d,]+) risk groups, ([\d,]+) rows", banding, re.M)
    assert documented, "frequency-banding.md states no holdout"
    groups, rows = (int(x.replace(",", "")) for x in documented.groups())
    assert scalar(holdout, "SELECT count(DISTINCT risk_group) FROM model.holdout WHERE risk_group_holdout") == groups
    assert scalar(holdout, "SELECT count(*) FROM model.holdout WHERE risk_group_holdout") == rows


def test_no_risk_group_is_on_both_sides_of_the_risk_group_split(holdout):
    assert scalar(
        holdout,
        "SELECT count(*) FROM (SELECT risk_group FROM model.holdout GROUP BY risk_group "
        "HAVING bool_or(risk_group_holdout) <> bool_and(risk_group_holdout)) g",
    ) == 0


def test_the_idpol_split_is_a_hash_python_reproduces(policies):
    expected = [int(hashlib.md5(str(i).encode()).hexdigest()[:8], 16) % 5 == 0 for i in policies["idpol"]]
    assert (policies["idpol_holdout"].to_numpy() == np.array(expected)).all(), "idpol_holdout differs from the md5 rule"


def test_split_runs_record_the_rows_they_were_fitted_on(runs):
    with runs.cursor() as cur:
        cur.execute("SELECT run, trained_on FROM model.glm_run")
        recorded = dict(cur.fetchall())
    for run, trained_on in recorded.items():
        expected = next((s for s in pp.TRAINING if s != "all" and run.endswith(s)), "all")
        assert trained_on == expected, run


def test_the_validation_run_balances_on_its_training_rows_only(runs, policies):
    result = pp.pure_premium(runs, pp.VALIDATION_RUN, policies)
    frame = result.frame
    assert result.run.trained_on == "risk_group_split"
    assert (frame["trained"] == ~frame["risk_group_holdout"]).all()
    assert abs(result.off_balance - 1) <= pp.LOSS_BALANCE_TOLERANCE
    with pytest.raises(pp.BasisError, match="claims per policy-year"):
        pp.check_claims_per_year(frame, "priced_claim_nb")


def test_document_was_generated_from_the_current_split(document, policies):
    stamped = re.search(r"frame md5 `([0-9a-f]{32})`", document)
    assert stamped and stamped.group(1) == canonical_md5(policies), REGENERATE
    both = int((policies["risk_group_holdout"] & policies["idpol_holdout"]).sum())
    assert f"scored on the {both:,} rows that both hold out" in document, REGENERATE


def test_document_names_the_validation_run(document):
    assert f"Validation run:\n`{pp.VALIDATION_RUN}`" in document, (
        f"pure_premium.VALIDATION_RUN is {pp.VALIDATION_RUN} but the document names another run"
    )
