from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd
import pytest

import pure_premium as pp
from conftest import scalar
from frame import canonical_md5

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "pure-premium.md"
REGENERATE = "re-run: .venv/Scripts/python scripts/pure_premium_report.py"

RUN = pp.Run("synthetic", "0" * 32, "priced_claim_nb", 1000.0, 1.5, "all")


@pytest.fixture
def consistent():
    return pd.DataFrame({
        "exposure": [0.5, 1.0, 0.25, 1.0],
        "claims_per_year": [2.0, 1.0, 4.0, 1.0],
        "priced_claim_nb": [1, 1, 1, 1],
        "capped_amount_per_claim": [100.0, 200.0, 100.0, 200.0],
        "capped_loss": [100.0, 200.0, 100.0, 200.0],
        "incurred_loss": [150.0, 300.0, 150.0, 300.0],
        "trained": [True, True, True, True],
    })


def test_a_consistent_frame_assembles_and_balances(consistent):
    result = pp.assemble(RUN, consistent)
    assert result.off_balance == pytest.approx(1)
    assert result.frame["expected_loss"].sum() == pytest.approx(consistent["incurred_loss"].sum())
    assert list(result.frame["amount_per_year"]) == pytest.approx([300, 300, 600, 300])


def test_checks_and_rebalancing_use_only_the_training_rows(consistent):
    held_out = pd.DataFrame({
        "exposure": [1.0], "claims_per_year": [1.0], "priced_claim_nb": [7], "capped_amount_per_claim": [200.0],
        "capped_loss": [9000.0], "incurred_loss": [50000.0], "trained": [False],
    })
    result = pp.assemble(RUN, pd.concat([consistent, held_out], ignore_index=True))
    assert result.off_balance == pytest.approx(1)
    assert result.frame["amount_per_year"].iloc[-1] == pytest.approx(300)


def test_expected_claims_passed_as_claims_per_year_are_refused(consistent):
    consistent["claims_per_year"] = consistent["exposure"] * consistent["claims_per_year"]
    with pytest.raises(pp.BasisError, match="claims per policy-year"):
        pp.assemble(RUN, consistent)


def test_a_severity_that_already_carries_the_load_is_refused(consistent):
    consistent["capped_amount_per_claim"] *= RUN.large_loss_load
    with pytest.raises(pp.BasisError, match="without the large-loss load"):
        pp.assemble(RUN, consistent)


def test_a_load_that_does_not_match_the_claims_is_refused(consistent):
    with pytest.raises(pp.BasisError, match="large-loss load"):
        pp.assemble(RUN._replace(large_loss_load=1.3), consistent)


def test_rebalancing_absorbs_a_small_gap_and_refuses_a_large_one(consistent):
    per_year = pd.Series([300.0, 300.0, 600.0, 300.0])
    assert pp.loss_off_balance(consistent, per_year * 1.005) == pytest.approx(1 / 1.005)
    with pytest.raises(pp.BasisError, match="off-balance factor"):
        pp.loss_off_balance(consistent, per_year * 1.02)


@pytest.fixture(scope="module")
def runs(staged):
    if scalar(staged, "SELECT to_regclass('model.glm_run')") is None:
        pytest.skip("model.glm_run not built; run scripts/migrate.py")
    if scalar(staged, "SELECT count(*) FROM model.glm_run") == 0:
        pytest.skip("no GLM predictions; run Rscript R/export_predictions.R")
    return staged


@pytest.fixture(scope="module")
def policies(runs):
    return pp.load_policies(runs)


@pytest.fixture(scope="module")
def priced(runs, policies):
    return pp.pure_premium(runs, pp.PRICING_RUN, policies)


@pytest.fixture(scope="module")
def document() -> str:
    if not DOCUMENT.exists():
        pytest.fail(f"{DOCUMENT.name} is missing; {REGENERATE}")
    return DOCUMENT.read_text(encoding="utf-8")


def test_policy_loss_reconciles_to_the_claims(staged):
    assert scalar(staged, "SELECT count(*) FROM model.policy_loss") == scalar(staged, "SELECT count(*) FROM fact.exposure")
    assert scalar(staged, "SELECT sum(priced_claim_nb) FROM model.policy_loss") == scalar(staged, "SELECT count(*) FROM fact.claim")
    assert scalar(staged, "SELECT round(sum(incurred_loss)::numeric, 2) FROM model.policy_loss") == scalar(
        staged, "SELECT sum(claim_amount) FROM fact.claim"
    )
    assert scalar(
        staged,
        "SELECT count(*) FROM model.policy_loss l JOIN fact.exposure e USING (idpol) WHERE l.priced_claim_nb > e.claim_nb",
    ) == 0


def test_the_pricing_run_counts_priced_claims(runs):
    assert scalar(runs, f"SELECT frequency_response FROM model.glm_run WHERE run = '{pp.PRICING_RUN}'") == "priced_claim_nb"


def test_the_pricing_run_balances_to_recorded_losses(priced):
    assert abs(priced.off_balance - 1) <= pp.LOSS_BALANCE_TOLERANCE
    assert priced.frame["expected_loss"].sum() == pytest.approx(priced.frame["incurred_loss"].sum(), rel=1e-9)


def test_the_reported_claims_run_is_refused(runs, policies):
    with pytest.raises(pp.BasisError, match="off-balance factor"):
        pp.pure_premium(runs, "reported_claims", policies)


def test_the_reported_claims_run_is_the_model_documented_in_d2_3(runs):
    banding = (REPO_ROOT / "docs" / "frequency-banding.md").read_text(encoding="utf-8")
    documented = re.search(r"^\| GLM relativity, band 0 against band 1 \| ([\d.]+) \|", banding, re.M)
    assert documented, "frequency-banding.md has no band 0 relativity"
    estimate = scalar(
        runs,
        "SELECT estimate FROM model.glm_coefficient "
        "WHERE run = 'reported_claims' AND model = 'frequency' AND term = 'veh_age_band1'",
    )
    assert f"{math.exp(-estimate):.2f}" == documented.group(1)


def test_predictions_from_another_frame_are_refused(runs, policies):
    changed = policies.copy()
    changed.loc[0, "exposure"] = changed.loc[0, "exposure"] / 2
    with pytest.raises(pp.BasisError, match="another frame"):
        pp.load_run(runs, pp.PRICING_RUN, changed)


def test_document_was_generated_from_the_current_predictions(document, policies, priced):
    stamped = re.search(r"frame md5 `([0-9a-f]{32})`", document)
    assert stamped and stamped.group(1) == canonical_md5(policies), REGENERATE
    factor = re.search(r"Off-balance factor: \*\*([\d.]+)\*\*", document)
    assert factor and factor.group(1) == f"{priced.off_balance:.6f}", REGENERATE


def test_document_states_the_tolerance_the_code_uses(document):
    assert f"within {pp.LOSS_BALANCE_TOLERANCE:.0%}\nof recorded losses" in document, (
        f"the code rebalances within {pp.LOSS_BALANCE_TOLERANCE:.0%} but the document states another tolerance"
    )
