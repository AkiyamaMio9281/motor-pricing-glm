"""Tests for foreign-key suspension and the indexes added in migration 005.

The suspension helpers drop constraints on a fact table, which is the kind of
code whose failure mode is not an error but a quietly weaker database. So the
tests are about exactly that: a round trip must restore definitions byte for
byte, and every way of leaving a constraint dropped must refuse to commit.

Each test works inside a savepoint and rolls back to it, so nothing here changes
the loaded mart.
"""

from __future__ import annotations

import psycopg
import pytest

from conftest import scalar


@pytest.fixture
def savepoint(staged):
    with staged.cursor() as cur:
        cur.execute("SAVEPOINT t")
    yield staged
    with staged.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT t")


def fk_definitions(conn, table: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND contype = 'f' ORDER BY conname",
            (table,),
        )
        return dict(cur.fetchall())


# ---------------------------------------------------------------------------
# Suspension
# ---------------------------------------------------------------------------

def test_round_trip_restores_every_definition_exactly(savepoint):
    before = fk_definitions(savepoint, "fact.exposure")
    assert len(before) == 5

    with savepoint.cursor() as cur:
        cur.execute("SELECT meta.suspend_foreign_keys('fact.exposure')")
        assert cur.fetchone()[0] == 5
    assert fk_definitions(savepoint, "fact.exposure") == {}

    with savepoint.cursor() as cur:
        cur.execute("SELECT meta.restore_foreign_keys('fact.exposure')")
        assert cur.fetchone()[0] == 5
    assert fk_definitions(savepoint, "fact.exposure") == before


def test_restored_constraints_still_enforce(savepoint):
    """Same text is not proof of the same behaviour; check a violation fails."""
    with savepoint.cursor() as cur:
        cur.execute("SELECT meta.suspend_foreign_keys('fact.exposure')")
        cur.execute("SELECT meta.restore_foreign_keys('fact.exposure')")
        cur.execute("SAVEPOINT probe")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "UPDATE fact.exposure SET region_key = 999 "
                "WHERE idpol = (SELECT min(idpol) FROM fact.exposure)"
            )
        cur.execute("ROLLBACK TO SAVEPOINT probe")


def test_a_forgotten_restore_refuses_to_commit(savepoint):
    """The failure this whole mechanism is most likely to produce."""
    with savepoint.cursor() as cur:
        cur.execute("SELECT meta.suspend_foreign_keys('fact.exposure')")
        cur.execute("SAVEPOINT probe")
        with pytest.raises(psycopg.errors.RaiseException, match="never restored"):
            cur.execute("SELECT meta.assert_foreign_keys_restored()")
        cur.execute("ROLLBACK TO SAVEPOINT probe")


def test_assertion_passes_once_everything_is_restored(savepoint):
    with savepoint.cursor() as cur:
        cur.execute("SELECT meta.suspend_foreign_keys('fact.exposure')")
        cur.execute("SELECT meta.suspend_foreign_keys('fact.claim')")
        cur.execute("SELECT meta.restore_foreign_keys('fact.exposure')")
        cur.execute("SAVEPOINT probe")
        with pytest.raises(psycopg.errors.RaiseException, match="fact.claim"):
            cur.execute("SELECT meta.assert_foreign_keys_restored()")
        cur.execute("ROLLBACK TO SAVEPOINT probe")
        cur.execute("SELECT meta.restore_foreign_keys('fact.claim')")
        cur.execute("SELECT meta.assert_foreign_keys_restored()")


def test_restoring_what_was_never_suspended_is_an_error(savepoint):
    with savepoint.cursor() as cur:
        cur.execute("SAVEPOINT probe")
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute("SELECT meta.restore_foreign_keys('fact.exposure')")
        cur.execute("ROLLBACK TO SAVEPOINT probe")


def test_definitions_survive_a_search_path_that_includes_dim(savepoint):
    """pg_get_constraintdef drops the schema when it is on the search path.

    The functions pin their own search path so that a stashed definition is
    always schema-qualified. Without that, suspending under this search path
    would stash REFERENCES region(region_key) and restore it against whatever
    'region' resolves to at restore time.
    """
    before = fk_definitions(savepoint, "fact.exposure")
    with savepoint.cursor() as cur:
        cur.execute("SET LOCAL search_path = dim, fact, public")
        cur.execute("SELECT meta.suspend_foreign_keys('fact.exposure')")
        cur.execute(
            "SELECT definition FROM pg_temp.suspended_foreign_keys "
            "ORDER BY constraint_name"
        )
        stashed = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT meta.restore_foreign_keys('fact.exposure')")
        cur.execute("RESET search_path")

    assert all("REFERENCES dim." in d for d in stashed), stashed
    assert fk_definitions(savepoint, "fact.exposure") == before


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "table, index, column",
    [
        ("fact.claim", "claim_idpol_idx", "idpol"),
        ("fact.exposure", "exposure_region_key_idx", "region_key"),
    ],
)
def test_index_exists_on_the_measured_column(staged, table, index, column):
    with staged.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'fact' "
            "AND indexname = %s",
            (index,),
        )
        row = cur.fetchone()
    assert row is not None, f"{index} missing"
    assert f"({column})" in row[0]


def test_every_referencing_foreign_key_column_is_indexed(staged):
    """Postgres indexes the referenced side of a foreign key, never this side.

    Without an index on the referencing column, removing a referenced row scans
    the referencing table once per row removed. Deleting all 643,953 claim-free
    policy-years took five and a half minutes without claim_idpol_idx and two and
    a half seconds with it. Any future fact table with a foreign key into another
    fact table has to carry the same index.
    """
    with staged.cursor() as cur:
        cur.execute(
            """
            SELECT c.conrelid::regclass::text, a.attname
            FROM pg_constraint c
            JOIN pg_attribute a
              ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
            WHERE c.contype = 'f'
              AND c.confrelid::regclass::text LIKE 'fact.%'
              AND NOT EXISTS (
                  SELECT 1 FROM pg_index i
                  WHERE i.indrelid = c.conrelid AND i.indkey[0] = c.conkey[1]
              )
            """
        )
        unindexed = cur.fetchall()
    assert unindexed == [], f"foreign keys into fact tables without an index: {unindexed}"


@pytest.mark.parametrize(
    "table",
    [
        "fact.exposure", "fact.claim",
        "dim.region", "dim.area", "dim.vehicle", "dim.driver_band", "dim.bonus_band",
    ],
)
def test_the_transform_leaves_planner_statistics_current(staged, table):
    """Check the statistics the planner uses, not the counter that reports on them.

    The first version of this test asserted n_mod_since_analyze == 0 and failed
    straight after a rebuild, although the statistics were correct. Transform
    004 runs ANALYZE inside its own transaction, and ANALYZE there does see the
    rows the transaction inserted: after a TRUNCATE sets reltuples to -1 and
    12,345 rows are inserted, an in-transaction ANALYZE sets it to exactly
    12,345. But the INSERT's modification count is only reported at commit,
    after the ANALYZE has reset the counter, so the counter reads as fully stale.
    Autovacuum then re-analyses about a minute later and zeroes it.

    So the old assertion was a race against autovacuum: red if the test ran in
    the first minute after a rebuild, green after. reltuples and pg_stats are
    what the planner reads, and they are right immediately.

    The dimensions are included because autovacuum will never analyse them.
    Its threshold is 50 modifications plus 10% of the table, and each rebuild
    truncates a dimension of 4 to 22 rows back to empty, so it never gets there.
    Before transform 004 analysed them explicitly, four of five had reltuples -1.
    """
    actual = scalar(staged, f"SELECT count(*) FROM {table}")
    estimated = scalar(
        staged, f"SELECT reltuples::bigint FROM pg_class WHERE oid = '{table}'::regclass"
    )
    assert estimated > 0, f"{table} has never been analysed (reltuples {estimated})"
    assert abs(estimated - actual) / actual < 0.01, (
        f"{table}: planner estimates {estimated} rows, table has {actual}"
    )

    schema, name = table.split(".")
    with staged.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_stats WHERE schemaname = %s AND tablename = %s",
            (schema, name),
        )
        assert cur.fetchone()[0] > 0, f"{table} has no column statistics"
