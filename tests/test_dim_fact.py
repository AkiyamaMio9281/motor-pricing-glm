"""Tests for the star schema built by transform 004.

The constraints in migration 004 do most of the enforcing, so these tests are
less about re-checking what a foreign key already refuses and more about the
things a constraint cannot express: that the row counts reconcile across the
layer boundary, that the band dimensions cover every observed value exactly
once, and that the claim grain produces the fan-out it is documented to.
"""

from __future__ import annotations

import pytest

from conftest import scalar


@pytest.fixture(scope="module")
def star(staged):
    if scalar(staged, "SELECT to_regclass('fact.exposure')") is None:
        pytest.skip("star schema not built; run scripts/migrate.py")
    assert scalar(staged, "SELECT count(*) FROM fact.exposure") > 0, (
        "staging is loaded but fact.exposure is empty: run scripts/transform.py"
    )
    return staged


# ---------------------------------------------------------------------------
# Reconciliation across the layer boundary
# ---------------------------------------------------------------------------

def test_every_staged_policy_year_reaches_the_fact_table(star):
    assert scalar(star, "SELECT count(*) FROM fact.exposure") == scalar(
        star, "SELECT count(*) FROM stg.policy_cleaned"
    ) == 678_013


def test_claims_reconcile_as_loaded_plus_excluded(star):
    """Staged claims split exactly into loaded and rejected, nothing lost."""
    staged_claims = scalar(star, "SELECT count(*) FROM stg.claim_cleaned")
    loaded = scalar(star, "SELECT count(*) FROM fact.claim")
    excluded = scalar(
        star,
        "SELECT rows_affected FROM stg.cleaning_audit "
        "WHERE table_name = 'fact.claim' "
        "AND rule_name = 'reject_claims_without_policy_year'",
    )
    assert staged_claims == 26_639
    assert excluded == 195
    assert loaded == 26_444
    assert loaded + excluded == staged_claims


def test_excluded_claims_are_exactly_the_six_orphan_policies(star):
    with star.cursor() as cur:
        cur.execute(
            "SELECT idpol, count(*) FROM stg.claim_cleaned c "
            "WHERE NOT EXISTS (SELECT 1 FROM fact.exposure e "
            "                  WHERE e.idpol = c.idpol) "
            "GROUP BY idpol ORDER BY count(*) DESC"
        )
        orphans = cur.fetchall()

    assert len(orphans) == 6
    assert orphans[0] == (2262511, 66), "the policy named in the FK error"
    assert sum(n for _, n in orphans) == 195


def test_no_claim_amount_was_lost_except_the_excluded_orphans(star):
    staged_total = scalar(star, "SELECT sum(claim_amount) FROM stg.claim_cleaned")
    loaded_total = scalar(star, "SELECT sum(claim_amount) FROM fact.claim")
    orphan_total = scalar(
        star,
        "SELECT sum(claim_amount) FROM stg.claim_cleaned c "
        "WHERE NOT EXISTS (SELECT 1 FROM fact.exposure e WHERE e.idpol = c.idpol)",
    )
    assert loaded_total + orphan_total == staged_total


# ---------------------------------------------------------------------------
# Constraints exist and are enforced
# ---------------------------------------------------------------------------

def test_every_fact_foreign_key_is_declared(star):
    """A constraint that was never created cannot fail; check they exist."""
    with star.cursor() as cur:
        cur.execute(
            "SELECT conrelid::regclass::text, confrelid::regclass::text "
            "FROM pg_constraint WHERE contype = 'f' "
            "AND connamespace = 'fact'::regnamespace"
        )
        declared = set(cur.fetchall())

    expected = {
        ("fact.exposure", "dim.region"),
        ("fact.exposure", "dim.area"),
        ("fact.exposure", "dim.vehicle"),
        ("fact.exposure", "dim.driver_band"),
        ("fact.exposure", "dim.bonus_band"),
        ("fact.claim", "fact.exposure"),
    }
    assert expected <= declared, f"missing: {expected - declared}"


def test_the_claim_foreign_key_refuses_an_orphan(star):
    """Prove the constraint enforces, rather than trusting that it was declared."""
    import psycopg

    with star.cursor() as cur:
        cur.execute("SAVEPOINT fk_probe")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO fact.claim (claim_key, idpol, claim_amount) "
                "VALUES (-1, 2262511, 1)"
            )
        cur.execute("ROLLBACK TO SAVEPOINT fk_probe")


# ---------------------------------------------------------------------------
# Band dimensions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value_column, band_table, low, high",
    [
        ("driv_age", "dim.driver_band", "age_min", "age_max"),
        ("bonus_malus", "dim.bonus_band", "bm_min", "bm_max"),
    ],
)
def test_every_observed_value_falls_in_exactly_one_band(
    star, value_column, band_table, low, high
):
    """No gap and no overlap, checked against the data rather than the ranges.

    A one-year gap in the driver bands silently drops 10,301 policies under an
    INNER JOIN. The transform uses LEFT JOINs so that a gap fails instead, but
    this test catches a gap or an overlap directly, before anything is loaded.
    """
    with star.cursor() as cur:
        cur.execute(
            f"SELECT v.val, count(b.*) FROM "
            f"(SELECT DISTINCT {value_column} AS val FROM stg.policy_cleaned) v "
            f"LEFT JOIN {band_table} b ON v.val BETWEEN b.{low} AND b.{high} "
            f"GROUP BY v.val HAVING count(b.*) <> 1"
        )
        bad = cur.fetchall()
    assert bad == [], f"values in 0 or 2+ bands: {bad[:10]}"


def test_bonus_bands_rise_monotonically_in_frequency(star):
    """The bands are anchored on the scale's meaning; the data should agree."""
    with star.cursor() as cur:
        cur.execute(
            "SELECT b.bonus_band_key, sum(e.claim_nb) / sum(e.exposure) "
            "FROM fact.exposure e JOIN dim.bonus_band b USING (bonus_band_key) "
            "GROUP BY b.bonus_band_key ORDER BY b.bonus_band_key"
        )
        freqs = [float(f) for _, f in cur.fetchall()]
    assert freqs == sorted(freqs), f"not monotone: {freqs}"


# ---------------------------------------------------------------------------
# Grain
# ---------------------------------------------------------------------------

def test_dimension_sizes(star):
    assert scalar(star, "SELECT count(*) FROM dim.region") == 22
    assert scalar(star, "SELECT count(*) FROM dim.area") == 6
    assert scalar(star, "SELECT count(*) FROM dim.vehicle") == 235
    assert scalar(star, "SELECT count(*) FROM dim.driver_band") == 8
    assert scalar(star, "SELECT count(*) FROM dim.bonus_band") == 4


def test_area_is_not_an_attribute_of_region(star):
    """Why area is its own dimension: every region spans several areas."""
    assert scalar(
        star,
        "SELECT count(*) FROM (SELECT region_key FROM fact.exposure "
        "GROUP BY region_key HAVING count(DISTINCT area_key) > 1) x",
    ) == 22


def test_area_density_ranges_overlap_only_at_the_boundaries(star):
    """Area cannot be recomputed from density; the source splits 50, 100, 500."""
    with star.cursor() as cur:
        cur.execute(
            "SELECT density FROM fact.exposure GROUP BY density "
            "HAVING count(DISTINCT area_key) > 1 ORDER BY density"
        )
        assert [row[0] for row in cur.fetchall()] == [50, 100, 500]


def test_joining_claims_to_exposure_fans_out(star):
    """Document the trap the claim grain exists to keep visible.

    Summing exposure over a claim-to-exposure join counts each policy-year once
    per claim. The correct total is only recoverable by aggregating claims
    first, which is what the mart does.
    """
    correct = scalar(
        star,
        "SELECT sum(exposure) FROM fact.exposure "
        "WHERE idpol IN (SELECT idpol FROM fact.claim)",
    )
    fanned = scalar(
        star,
        "SELECT sum(e.exposure) FROM fact.claim c "
        "JOIN fact.exposure e USING (idpol)",
    )
    assert fanned > correct, "no fan-out: claim grain may have been aggregated"
