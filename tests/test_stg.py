"""Tests for the staging layer and its audit trail.

The audit table's whole value is that it is an accounting rather than a
description. If `rows_out` were written by hand, or drifted from what the rules
actually removed, it would still look exactly as convincing. So the first test
here re-derives the chain and checks it closes, and the rest check that the
claims the chain makes about the data are true.
"""

from __future__ import annotations

from conftest import scalar


def test_audit_chain_closes_for_every_table(staged):
    """rows_out must equal the previous rows_out minus what the rule removed."""
    with staged.cursor() as cur:
        cur.execute("SELECT DISTINCT table_name FROM stg.cleaning_audit")
        tables = [row[0] for row in cur.fetchall()]

    assert tables, "audit table is empty"

    for table in tables:
        with staged.cursor() as cur:
            cur.execute(
                "SELECT rule_seq, rule_name, kind, rows_affected, rows_out "
                "FROM stg.cleaning_audit WHERE table_name = %s "
                "ORDER BY rule_seq",
                (table,),
            )
            rules = cur.fetchall()

        previous = None
        for seq, name, kind, affected, out in rules:
            if previous is None:
                previous = out
                continue
            expected = previous - affected if kind == "reject" else previous
            assert out == expected, (
                f"{table} rule {seq} {name}: rows_out {out}, expected {expected}"
            )
            previous = out

        assert previous == scalar(staged, f"SELECT count(*) FROM {table}"), (
            f"{table}: final rows_out does not match the table"
        )


def test_observations_never_remove_rows(staged):
    with staged.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM stg.cleaning_audit a WHERE a.kind = 'observe' "
            "AND a.rows_out <> ("
            "  SELECT b.rows_out FROM stg.cleaning_audit b "
            "  WHERE b.table_name = a.table_name AND b.rule_seq < a.rule_seq "
            "  ORDER BY b.rule_seq DESC LIMIT 1)"
        )
        assert cur.fetchone()[0] == 0


def test_scientific_notation_id_became_one_hundred_thousand(staged):
    """The upstream file writes one policy id as 1e+05.

    Routing the cast through numeric resolves it to 100000. The row must exist
    exactly once and must not have collided with an existing identifier, which
    is why the count is checked rather than just the presence.
    """
    assert scalar(
        staged, "SELECT count(*) FROM stg.policy_cleaned WHERE idpol = 100000"
    ) == 1
    assert scalar(
        staged,
        "SELECT count(*) FROM stg.cleaning_audit "
        "WHERE table_name = 'stg.policy_cleaned' AND rule_seq = 1 "
        "AND rows_affected = 1",
    ) == 1


def test_no_row_survived_that_a_reject_rule_targets(staged):
    """The rules and the constraints must agree.

    A rule that reports zero removals because its predicate is wrong looks the
    same in the audit table as one that found clean data. Re-asserting the
    predicates against the finished table is what separates the two.
    """
    assert scalar(
        staged, "SELECT count(*) FROM stg.policy_cleaned WHERE exposure <= 0"
    ) == 0
    assert scalar(
        staged, "SELECT count(*) FROM stg.policy_cleaned WHERE claim_nb < 0"
    ) == 0
    assert scalar(
        staged,
        "SELECT count(*) FROM stg.policy_cleaned "
        "WHERE area NOT IN ('A','B','C','D','E','F') "
        "   OR veh_gas NOT IN ('Regular','Diesel')",
    ) == 0
    assert scalar(
        staged, "SELECT count(*) FROM stg.claim_cleaned WHERE claim_amount <= 0"
    ) == 0


def test_policy_grain_is_one_row_per_policy(staged):
    total = scalar(staged, "SELECT count(*) FROM stg.policy_cleaned")
    distinct = scalar(staged, "SELECT count(DISTINCT idpol) FROM stg.policy_cleaned")
    assert total == distinct == 678_013


def test_claim_grain_is_not_aggregated(staged):
    """Claims must stay at claim grain, with idpol repeating.

    Collapsing them here would hide the fan-out that the fact layer has to
    handle, and would silently change what a join to exposure means.
    """
    total = scalar(staged, "SELECT count(*) FROM stg.claim_cleaned")
    distinct = scalar(staged, "SELECT count(DISTINCT idpol) FROM stg.claim_cleaned")
    assert total == 26_639
    assert distinct == 24_950
    assert distinct < total, "claim grain has been aggregated away"


def test_recorded_observations_match_a_fresh_count(staged):
    """Re-derive each observation independently of the migration that wrote it."""
    with staged.cursor() as cur:
        cur.execute(
            "SELECT rule_name, rows_affected FROM stg.cleaning_audit "
            "WHERE kind = 'observe'"
        )
        recorded = dict(cur.fetchall())

    assert recorded["observe_exposure_above_one"] == scalar(
        staged, "SELECT count(*) FROM stg.policy_cleaned WHERE exposure > 1"
    )
    assert recorded["observe_orphan_claims"] == scalar(
        staged,
        "SELECT count(*) FROM stg.claim_cleaned c WHERE NOT EXISTS ("
        "  SELECT 1 FROM stg.policy_cleaned p WHERE p.idpol = c.idpol)",
    )
    assert recorded["observe_claims_without_amount"] == scalar(
        staged,
        "SELECT count(*) FROM stg.policy_cleaned p WHERE p.claim_nb > 0 "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM stg.claim_cleaned c WHERE c.idpol = p.idpol)",
    )


def test_severity_file_prices_only_part_of_the_reported_claims(staged):
    """Pin the shortfall that decides how the severity model is populated.

    Roughly a quarter of reported claims have no amount. The severity model is
    therefore fitted on a different effective population from the frequency
    model, and restricting it to claim_nb > 0 does not change that.
    """
    reported = scalar(
        staged, "SELECT sum(claim_nb) FROM stg.policy_cleaned WHERE claim_nb > 0"
    )
    unpriced = scalar(
        staged,
        "SELECT sum(claim_nb) FROM stg.policy_cleaned p WHERE p.claim_nb > 0 "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM stg.claim_cleaned c WHERE c.idpol = p.idpol)",
    )
    assert reported == 36_102
    assert unpriced == 9_657
    assert 0.26 < unpriced / reported < 0.27
