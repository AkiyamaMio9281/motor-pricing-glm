"""Tests for the severity frame, fit_severity()'s guards, and the population document.

The guard tests run R on synthetic claims, with no database. They are the direct
test of the pitfall this layer exists to prevent: a Gamma fit must never be handed
a policy without an amount, and when one arrives the failure must be an error that
names it, not glm()'s default of dropping the row and saying nothing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from conftest import scalar
from frame import canonical_md5, load_frequency_frame
from r_runtime import find_rscript

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "severity-population.md"
REGENERATE = "re-run: Rscript R/severity_population.R"


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def severity(staged):
    if scalar(staged, "SELECT to_regclass('model.severity_frame')") is None:
        pytest.skip("severity frame not built; run scripts/migrate.py")
    return staged


def test_one_row_per_priced_claim(severity):
    assert scalar(severity, "SELECT count(*) FROM model.severity_frame") == scalar(
        severity, "SELECT count(*) FROM fact.claim"
    ) == 26_444


def test_no_zero_claim_policy_reaches_the_frame(severity):
    assert scalar(severity, "SELECT count(*) FROM model.severity_frame WHERE claim_nb = 0") == 0


def test_no_unpriced_policy_reaches_the_frame(severity):
    """Policies reporting claims without an amount have no row, by construction."""
    assert scalar(
        severity,
        "SELECT count(*) FROM model.frequency_frame f WHERE f.claim_nb > 0 "
        "AND NOT EXISTS (SELECT 1 FROM fact.claim c WHERE c.idpol = f.idpol) "
        "AND EXISTS (SELECT 1 FROM model.severity_frame s WHERE s.idpol = f.idpol)",
    ) == 0
    assert scalar(severity, "SELECT count(DISTINCT idpol) FROM model.severity_frame") == 24_944


def test_every_amount_is_positive_and_a_float(severity):
    assert scalar(severity, "SELECT count(*) FROM model.severity_frame WHERE claim_amount <= 0") == 0
    with severity.cursor() as cur:
        cur.execute("SELECT claim_amount FROM model.severity_frame LIMIT 1")
        assert isinstance(cur.fetchone()[0], float)


def test_rating_factors_come_from_the_frequency_frame(severity):
    assert scalar(
        severity,
        "SELECT count(*) FROM model.severity_frame s JOIN model.frequency_frame f USING (idpol) "
        "WHERE (s.area, s.region, s.veh_brand, s.veh_gas, s.veh_power, s.veh_age, "
        "       s.driv_age, s.bonus_malus, s.density) "
        "   IS DISTINCT FROM (f.area, f.region, f.veh_brand, f.veh_gas, f.veh_power, "
        "       f.veh_age, f.driv_age, f.bonus_malus, f.density)",
    ) == 0


# ---------------------------------------------------------------------------
# fit_severity() guards, in R, on synthetic claims
# ---------------------------------------------------------------------------

GUARD_SCRIPT = r"""
source("R/bands.R"); source("R/severity.R")
probe <- function(label, expr) {
  message <- tryCatch({ force(expr); "no error" }, error = function(e) conditionMessage(e))
  cat(label, "\t", message, "\n", sep = "")
}
set.seed(1)
ok <- data.frame(claim_amount = rgamma(200, shape = 2, rate = 0.001))
probe("missing", fit_severity(data.frame(claim_amount = c(ok$claim_amount, NA)), character(0)))
probe("zero", fit_severity(data.frame(claim_amount = c(ok$claim_amount, 0)), character(0)))
probe("negative", fit_severity(data.frame(claim_amount = c(ok$claim_amount, -5)), character(0)))
probe("no_terms", fit_severity(ok))
fit <- fit_severity(ok, character(0))
cat("valid\t", sprintf("%.4f", exp(coef(fit)[[1]]) / mean(ok$claim_amount)), "\t", nobs(fit), "\n", sep = "")
"""


@pytest.fixture(scope="module")
def guards(tmp_path_factory) -> dict[str, list[str]]:
    rscript = find_rscript()
    if rscript is None:
        pytest.skip("Rscript not found")
    # Written to a file rather than passed with -e: on Windows, Rscript given a
    # multi-line -e argument exits with an access violation and no output at all.
    script = tmp_path_factory.mktemp("severity_guards") / "guards.R"
    script.write_bytes(GUARD_SCRIPT.encode("utf-8"))
    completed = subprocess.run(
        [str(rscript), str(script)], cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return {
        line.split("\t")[0]: line.split("\t")[1:]
        for line in completed.stdout.splitlines() if "\t" in line
    }


def test_a_missing_amount_is_refused_not_dropped(guards):
    assert "no amount" in guards["missing"][0], guards["missing"]


def test_a_zero_amount_is_refused(guards):
    assert "non-positive" in guards["zero"][0], guards["zero"]


def test_a_negative_amount_is_refused(guards):
    assert "non-positive" in guards["negative"][0], guards["negative"]


def test_there_is_no_default_specification(guards):
    assert "no default terms" in guards["no_terms"][0], guards["no_terms"]


def test_a_valid_constant_fit_uses_every_claim_and_matches_the_mean(guards):
    ratio, used = guards["valid"]
    assert float(ratio) == pytest.approx(1.0, abs=1e-4)
    assert int(used) == 200


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def document() -> str:
    if not DOCUMENT.exists():
        pytest.fail(f"{DOCUMENT.name} is missing; {REGENERATE}")
    return DOCUMENT.read_text(encoding="utf-8")


def test_document_was_generated_from_the_current_data(severity, document):
    stamped = re.search(r"frame md5 `([0-9a-f]{32})`", document)
    assert stamped and stamped.group(1) == canonical_md5(load_frequency_frame(severity)), REGENERATE
    claims = re.search(r"([\d,]+) priced claims totalling ([\d,]+\.\d{2})", document)
    assert claims, "document does not state its claim count and total"
    assert int(claims.group(1).replace(",", "")) == scalar(severity, "SELECT count(*) FROM fact.claim")
    total = scalar(severity, "SELECT round(sum(claim_amount), 2) FROM fact.claim")
    assert float(claims.group(2).replace(",", "")) == pytest.approx(float(total), abs=0.005), REGENERATE


def test_the_folds_still_disagree_about_the_specification(document):
    """The document declines the single-fold verdict because folds change sign."""
    section = document.split("| Holdout fold |", 1)[1]
    differences = [
        float(row.split("|")[5].strip().replace(",", ""))
        for row in section.splitlines()[2:7]
    ]
    assert len(differences) == 5
    assert min(differences) < 0 < max(differences), (
        f"the folds now agree ({differences}); the specification section's conclusion no longer holds"
    )
