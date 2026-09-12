"""Tests for the pricing decisions in transform 003.

Two things need holding down here, and they are different in kind.

The mechanical ones: the cap is applied where it should be and nowhere else,
the original exposure survives untouched, and every policy gets exactly one
adjustment row. These would break loudly if the SQL were wrong.

The substantive one: the short-exposure threshold is justified by a measured
departure from proportionality, and that measurement is re-derived here rather
than trusted. A threshold with a number in a comment beside it and no test is a
threshold that can quietly stop matching the data it was chosen from.
"""

from __future__ import annotations

from conftest import scalar


def test_every_policy_has_exactly_one_adjustment_row(staged):
    assert scalar(staged, "SELECT count(*) FROM stg.policy_adjusted") == 678_013
    assert scalar(
        staged, "SELECT count(DISTINCT idpol) FROM stg.policy_adjusted"
    ) == 678_013
    assert scalar(
        staged,
        "SELECT count(*) FROM stg.policy_cleaned c "
        "WHERE NOT EXISTS (SELECT 1 FROM stg.policy_adjusted a "
        "                  WHERE a.idpol = c.idpol)",
    ) == 0


def test_the_cap_is_applied_exactly_where_exposure_exceeded_one(staged):
    assert scalar(
        staged, "SELECT count(*) FROM stg.policy_adjusted WHERE exposure_adj > 1"
    ) == 0
    assert scalar(
        staged,
        "SELECT count(*) FROM stg.policy_cleaned c "
        "JOIN stg.policy_adjusted a USING (idpol) "
        "WHERE a.is_exposure_capped <> (c.exposure > 1)",
    ) == 0
    assert scalar(
        staged,
        "SELECT count(*) FROM stg.policy_cleaned c "
        "JOIN stg.policy_adjusted a USING (idpol) "
        "WHERE a.exposure_adj <> LEAST(c.exposure, 1)",
    ) == 0


def test_the_uncapped_exposure_is_still_available(staged):
    """The decision must stay reversible without rebuilding from the CSV."""
    assert scalar(
        staged, "SELECT count(*) FROM stg.policy_cleaned WHERE exposure > 1"
    ) == 1_224
    assert float(
        scalar(staged, "SELECT max(exposure) FROM stg.policy_cleaned")
    ) == 2.01


def test_capping_costs_what_the_decision_claims(staged):
    """139 exposure-years and no claims; dropping would cost 1,363 and 54."""
    lost = scalar(
        staged,
        "SELECT round(sum(c.exposure) - sum(a.exposure_adj)) "
        "FROM stg.policy_cleaned c JOIN stg.policy_adjusted a USING (idpol)",
    )
    assert lost == 139

    dropped_exposure = scalar(
        staged,
        "SELECT round(sum(exposure)) FROM stg.policy_cleaned WHERE exposure > 1",
    )
    dropped_claims = scalar(
        staged,
        "SELECT sum(claim_nb) FROM stg.policy_cleaned WHERE exposure > 1",
    )
    assert dropped_exposure == 1_363
    assert dropped_claims == 54


def test_claim_counts_are_flagged_and_never_capped(staged):
    """The nine extreme policies must keep their counts."""
    assert scalar(
        staged, "SELECT max(claim_nb) FROM stg.policy_cleaned"
    ) == 16
    assert scalar(
        staged, "SELECT count(*) FROM stg.policy_adjusted WHERE is_high_claim_count"
    ) == 9
    assert scalar(
        staged,
        "SELECT count(*) FROM stg.policy_cleaned c "
        "JOIN stg.policy_adjusted a USING (idpol) "
        "WHERE a.is_high_claim_count <> (c.claim_nb > 4)",
    ) == 0


def test_proportionality_fails_at_short_exposure(staged):
    """Re-derive the measurement the short-exposure threshold rests on.

    If expected claims were proportional to exposure, claims per exposure-year
    would be flat across bands at the portfolio rate. The flag exists because
    it is not, and this test fails if that stops being true of the data.
    """
    portfolio = float(scalar(
        staged,
        "SELECT sum(c.claim_nb) / sum(a.exposure_adj) "
        "FROM stg.policy_cleaned c JOIN stg.policy_adjusted a USING (idpol)",
    ))
    assert 0.09 < portfolio < 0.11

    short_band = float(scalar(
        staged,
        "SELECT sum(c.claim_nb) / sum(a.exposure_adj) "
        "FROM stg.policy_cleaned c JOIN stg.policy_adjusted a USING (idpol) "
        "WHERE a.is_short_exposure",
    ))
    assert short_band > 3, "short-exposure band no longer departs from the portfolio"
    assert short_band / portfolio > 30, (
        "the flag is justified by a large departure; it has shrunk"
    )


def test_the_short_exposure_band_is_small_in_exposure_and_not_in_claims(staged):
    """The shape that makes these rows high-leverage rather than merely rare."""
    with staged.cursor() as cur:
        cur.execute(
            "SELECT count(*), round(sum(a.exposure_adj)), sum(c.claim_nb) "
            "FROM stg.policy_cleaned c JOIN stg.policy_adjusted a USING (idpol) "
            "WHERE a.is_short_exposure"
        )
        policies, exposure_years, claims = cur.fetchone()
    exposure_years = float(exposure_years)

    assert policies == 13_603
    assert exposure_years == 101
    assert claims == 362

    total_exposure = float(
        scalar(staged, "SELECT sum(exposure_adj) FROM stg.policy_adjusted")
    )
    total_claims = float(
        scalar(staged, "SELECT sum(claim_nb) FROM stg.policy_cleaned")
    )
    assert exposure_years / total_exposure < 0.001
    assert claims / total_claims > 0.009


def test_implied_rate_matches_its_definition(staged):
    """One definition, computed once, so diagnostics cannot each pick their own."""
    assert scalar(
        staged,
        "SELECT count(*) FROM stg.policy_cleaned c "
        "JOIN stg.policy_adjusted a USING (idpol) "
        "WHERE a.implied_rate <> c.claim_nb / a.exposure_adj",
    ) == 0
    assert scalar(
        staged, "SELECT round(max(implied_rate)) FROM stg.policy_adjusted"
    ) == 732


def test_no_decision_removed_a_row(staged):
    """003 caps and flags. Anything that rejects belongs in 002."""
    with staged.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM stg.cleaning_audit "
            "WHERE table_name = 'stg.policy_adjusted' AND kind = 'reject'"
        )
        assert cur.fetchone()[0] == 0

        cur.execute(
            "SELECT DISTINCT rows_out FROM stg.cleaning_audit "
            "WHERE table_name = 'stg.policy_adjusted'"
        )
        assert [row[0] for row in cur.fetchall()] == [678_013]
