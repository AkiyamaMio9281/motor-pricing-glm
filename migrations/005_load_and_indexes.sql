-- 005 · foreign-key suspension for bulk loads, and the two indexes that earned
--        their place
--
-- Both halves come from EXPLAIN ANALYZE. Current measured values live in
-- docs/explain-plans.md, which scripts/explain_plans.py regenerates by timing
-- each query with and without its index inside rolled-back transactions.
--
-- The figures in these comments are deliberately rounded to what held across
-- two separate measurement sessions. Sub-second timings move by a factor of two
-- or three between sessions, and a migration is frozen once applied, so it
-- records the reasoning and the stable magnitudes, and the regenerated document
-- records the numbers.

CREATE SCHEMA IF NOT EXISTS meta;

-- ---------------------------------------------------------------------------
-- Suspending foreign keys during a bulk load
-- ---------------------------------------------------------------------------
--
-- Loading 678,013 rows into fact.exposure took about ten seconds. EXPLAIN
-- ANALYZE on the INSERT attributes three quarters of that to five foreign-key
-- triggers, each firing once per row: 678,013 lookups into a dimension, five
-- times over.
--
-- Dropping the five constraints, loading, and adding them back validates each
-- one with a single set-based query instead. Across both sessions that was
-- about ten seconds down to about two, 4.7x to 5.7x, and the set-based
-- validation of all five constraints took under 200 ms of it, for the same
-- guarantee: a key that points at nothing still fails the transform, it just
-- fails at the end instead of on the row.
--
-- The obvious implementation writes the constraint definitions into the
-- transform, and that is the one to avoid. Two copies of a definition drift:
-- change a foreign key in a migration and the transform quietly re-creates the
-- old one. So these functions read the definitions out of pg_constraint before
-- dropping them and restore exactly what they read. The migration stays the only
-- place a constraint is defined.
--
-- The risk this introduces is a constraint that is dropped and never restored.
-- Three things close it. The whole suspend-load-restore sequence runs inside the
-- transform's single transaction, so an error anywhere rolls the DROP back too.
-- meta.assert_foreign_keys_restored() raises if anything is still stashed, so a
-- forgotten restore fails the transform instead of committing without its
-- constraints. And tests/test_dim_fact.py checks the constraints exist
-- afterwards.
--
-- search_path is pinned to pg_catalog inside each function. pg_get_constraintdef
-- qualifies a referenced table only when its schema is not on the search path,
-- so under a search path that included dim, a stashed definition would read
-- REFERENCES region(region_key) and could be restored against the wrong table.

CREATE OR REPLACE FUNCTION meta.suspend_foreign_keys(p_table regclass)
RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $fn$
DECLARE
    r record;
    n integer := 0;
BEGIN
    CREATE TEMP TABLE IF NOT EXISTS suspended_foreign_keys (
        table_name      regclass NOT NULL,
        constraint_name name     NOT NULL,
        definition      text     NOT NULL,
        PRIMARY KEY (table_name, constraint_name)
    ) ON COMMIT DROP;

    FOR r IN
        SELECT conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = p_table AND contype = 'f'
        ORDER BY conname
    LOOP
        INSERT INTO pg_temp.suspended_foreign_keys
        VALUES (p_table, r.conname, r.def);
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', p_table, r.conname);
        n := n + 1;
    END LOOP;

    RETURN n;
END;
$fn$;

CREATE OR REPLACE FUNCTION meta.restore_foreign_keys(p_table regclass)
RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_clauses text;
    n         integer;
BEGIN
    IF to_regclass('pg_temp.suspended_foreign_keys') IS NULL THEN
        RAISE EXCEPTION 'no foreign keys were suspended in this transaction';
    END IF;

    SELECT count(*),
           string_agg(format('ADD CONSTRAINT %I %s', constraint_name, definition),
                      ', ' ORDER BY constraint_name)
    INTO n, v_clauses
    FROM pg_temp.suspended_foreign_keys
    WHERE table_name = p_table;

    IF n = 0 THEN
        RAISE EXCEPTION 'no suspended foreign keys are recorded for %', p_table;
    END IF;

    -- One ALTER TABLE for all of them: one lock acquisition, and each
    -- constraint is validated by its own set-based query.
    EXECUTE format('ALTER TABLE %s %s', p_table, v_clauses);

    DELETE FROM pg_temp.suspended_foreign_keys WHERE table_name = p_table;
    RETURN n;
END;
$fn$;

CREATE OR REPLACE FUNCTION meta.assert_foreign_keys_restored()
RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $fn$
DECLARE
    v_left text;
BEGIN
    IF to_regclass('pg_temp.suspended_foreign_keys') IS NULL THEN
        RETURN;
    END IF;

    SELECT string_agg(format('%s.%s', table_name, constraint_name), ', ')
    INTO v_left
    FROM pg_temp.suspended_foreign_keys;

    IF v_left IS NOT NULL THEN
        RAISE EXCEPTION
            'foreign keys suspended and never restored: %. Refusing to commit a fact table without its constraints.',
            v_left;
    END IF;
END;
$fn$;

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
--
-- Postgres indexes the referenced side of a foreign key, because that side must
-- be a primary key or unique constraint. It does not index the referencing
-- side. fact.claim.idpol references fact.exposure, and had no index.
--
-- What that costs is invisible to every query that reads claims in bulk and
-- enormous for anything that removes a policy-year: deleting a row from
-- fact.exposure must confirm no claim still references it, and without an index
-- that confirmation is a sequential scan of fact.claim, once per deleted row.
--
--   delete 1,272 claim-free policy-years in one region   ~650 ms  ->  under 30 ms
--   delete all 643,953 claim-free policy-years        328,607 ms  ->  2,546 ms
--
-- The second line is a single run of each, not part of the regenerated
-- document, because the slow side takes five and a half minutes. It is 129x,
-- and the whole difference is in the foreign-key trigger: 0.51 ms per deleted
-- row, which is one sequential scan of 26,444 claims each time. The mart is rebuilt by
-- TRUNCATE, which skips the check, so this is not on today's hot path. It is the
-- index whose absence turns the first correction to a region into a full scan of
-- the claims table for every row removed.
CREATE INDEX claim_idpol_idx ON fact.claim (idpol);

-- For drilldowns into one region, which is what the Power BI relativity page
-- does. It helps exactly when the filter is selective, and the measurement says
-- where that stops:
--
--   region R43, 0.2% of policies     about 10 ms  ->  under 1 ms
--   region R24, 23.7% of policies    no gain in either session
--
-- The planner reads a quarter of the table either way. A covering index with
-- INCLUDE (vehicle_key, exposure, claim_nb) was also measured. It bought 13% on
-- the broad region where the plain index bought nothing, and it is 26 MB against
-- the plain index's 4.6 MB, 5.8x. The gap is that large because a B-tree
-- deduplicates repeated keys, which leaves an index over 22 distinct regions very
-- small, and an index with INCLUDE columns cannot be deduplicated. Half the size
-- of the 50 MB table, for 13% on one query shape. Rejected.
CREATE INDEX exposure_region_key_idx ON fact.exposure (region_key);
