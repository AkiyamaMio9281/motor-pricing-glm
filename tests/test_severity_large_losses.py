"""Checks for the large-loss cap, the chosen severity terms, and their document.

The cap and the severity terms are constants in R/severity.R, chosen by
R/severity_large_losses.R. These tests keep the constants, the data and the
document from drifting apart without anyone re-running the analysis:

  the cap         LARGE_LOSS_CAP must still be the 99.5th percentile of the
                  loaded claims, computed independently in Postgres
  the terms       SEVERITY_TERMS must be what the document chose, and the document's
                  own fold table must show that choice winning
  the verdict     the flat-load verdict must agree with the interval printed beside it
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import r_vector, scalar
from frame import canonical_md5, load_frequency_frame

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "severity-large-losses.md"
SEVERITY_R = REPO_ROOT / "R" / "severity.R"
REGENERATE = "re-run: Rscript R/severity_large_losses.R"


def r_scalar(path: Path, name: str) -> float:
    match = re.search(rf"^{re.escape(name)}\s*<-\s*([0-9.]+)\s*$", path.read_text(encoding="utf-8"), re.M)
    assert match, f"could not find {name} in {path.name}"
    return float(match.group(1))


@pytest.fixture(scope="module")
def document() -> str:
    if not DOCUMENT.exists():
        pytest.fail(f"{DOCUMENT.name} is missing; {REGENERATE}")
    return DOCUMENT.read_text(encoding="utf-8")


def test_the_cap_is_still_the_995th_percentile_of_the_claims(staged):
    """percentile_cont interpolates linearly, the same definition as R's type 7."""
    cap = r_scalar(SEVERITY_R, "LARGE_LOSS_CAP")
    percentile = scalar(staged, "SELECT percentile_cont(0.995) WITHIN GROUP (ORDER BY claim_amount) FROM fact.claim")
    assert round(float(percentile)) == cap, (
        f"LARGE_LOSS_CAP is {cap:.0f} but the claims' 99.5th percentile is now {percentile:.2f}"
    )


def test_the_document_uses_the_same_cap(document):
    cap = r_scalar(SEVERITY_R, "LARGE_LOSS_CAP")
    assert f"99.5th percentile, {cap:,.0f}" in document, REGENERATE


def test_document_was_generated_from_the_current_data(staged, document):
    stamped = re.search(r"frame md5 `([0-9a-f]{32})`", document)
    assert stamped and stamped.group(1) == canonical_md5(load_frequency_frame(staged)), REGENERATE
    total = scalar(staged, "SELECT round(sum(claim_amount), 2) FROM fact.claim")
    assert f"totalling {float(total):,.2f}" in document, REGENERATE


def test_the_model_uses_the_terms_the_document_chose(document):
    chosen = re.search(r"^Chosen terms: `(.+)`$", document, re.M)
    assert chosen, "document names no chosen terms"
    terms = r_vector(SEVERITY_R, "SEVERITY_TERMS")
    expected = " + ".join(terms) if terms else "1"
    assert chosen.group(1) == expected, (
        f"SEVERITY_TERMS is {expected!r} but the analysis chose {chosen.group(1)!r}"
    )


def test_the_chosen_terms_win_the_fold_table(document):
    total = re.search(r"^\| \*\*total\*\* \| [\d,]+ \| ([\d,.]+) \| ([\d,.]+) \|", document, re.M)
    assert total, "no total row in the fold table"
    constant, rated = (float(x.replace(",", "")) for x in total.groups())
    says_rated = "Chosen by the rule: **the frequency terms**" in document
    assert says_rated == (rated < constant), (
        f"document says rated={says_rated}, but totals are constant {constant} and rated {rated}"
    )


def test_the_flat_load_verdict_matches_its_interval(document):
    interval = re.search(r"95% interval (-?[\d.]+) to (-?[\d.]+)", document)
    assert interval, "no interval for the large-claim slope"
    low, high = (float(x) for x in interval.groups())
    rejected = low > 0 or high < 0
    verdict = "rejected" if rejected else "not rejected"
    assert ("**The flat load is rejected by the rule.**" in document) == rejected, (
        f"interval {low} to {high} means the flat load is {verdict}, but the document says otherwise"
    )
    assert ("**The flat load is not rejected by the rule" in document) == (not rejected), (
        f"interval {low} to {high} means the flat load is {verdict}, but the document says otherwise"
    )
