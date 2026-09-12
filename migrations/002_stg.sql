-- 002 · staging structure: the audit trail, and the tables the rules fill
--
-- Structure only. The rules that populate these tables live in
-- sql/transform/002_stg.sql and are run by scripts/transform.py.
--
-- The split is not tidiness. A migration that both creates a table and fills it
-- from another table is only correct when the source happens to be loaded, and
-- `migrate --reset` runs before any load. See DEVLOG: the first version of this
-- file built a staging layer from an empty raw layer and wrote a seven-rule
-- audit trail that reconciled perfectly while describing nothing.
--
-- So: anything here is safe to run against an empty database. Anything that
-- reads data is a transform, is re-runnable, and truncates before it writes.
--
-- The staging tables carry no CHECK constraints on purpose. The rules work by
-- inserting everything and then deleting per rule, which is what makes the
-- row-count chain in stg.cleaning_audit reconcile step by step; a constraint
-- would reject the offending rows at insert time and there would be nothing
-- left for a rule to count. Constraints belong on the star schema in 004, where
-- the data has already been through the rules.

CREATE SCHEMA IF NOT EXISTS stg;

-- ---------------------------------------------------------------------------
-- Audit trail
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS stg.cleaning_audit;
CREATE TABLE stg.cleaning_audit (
    rule_seq      integer     NOT NULL,
    rule_name     text        NOT NULL,
    kind          text        NOT NULL
                  CHECK (kind IN ('transform', 'reject', 'observe')),
    table_name    text        NOT NULL,
    rows_affected bigint      NOT NULL,
    rows_out      bigint      NOT NULL,
    note          text,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_name, rule_seq)
);

COMMENT ON TABLE stg.cleaning_audit IS
    'One row per cleaning rule. transform changes values, reject removes rows, observe only counts. rows_out is the table size after the rule ran, so consecutive rows reconcile.';

-- rows_out is measured here rather than passed in. A caller that reports its
-- own idea of the resulting size can be wrong and look exactly as convincing;
-- counting the table at the moment the rule finishes cannot.
CREATE OR REPLACE FUNCTION stg.record_rule(
    p_seq integer, p_name text, p_kind text, p_table text,
    p_affected bigint, p_note text DEFAULT NULL
) RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE
    v_out bigint;
BEGIN
    EXECUTE format('SELECT count(*) FROM %s', p_table) INTO v_out;
    INSERT INTO stg.cleaning_audit
        (rule_seq, rule_name, kind, table_name, rows_affected, rows_out, note)
    VALUES (p_seq, p_name, p_kind, p_table, p_affected, v_out, p_note);
END;
$fn$;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS stg.policy_cleaned;
CREATE TABLE stg.policy_cleaned (
    idpol       bigint  NOT NULL,
    claim_nb    integer NOT NULL,
    exposure    numeric NOT NULL,
    area        text    NOT NULL,
    veh_power   integer NOT NULL,
    veh_age     integer NOT NULL,
    driv_age    integer NOT NULL,
    bonus_malus integer NOT NULL,
    veh_brand   text    NOT NULL,
    veh_gas     text    NOT NULL,
    density     integer NOT NULL,
    region      text    NOT NULL
);

COMMENT ON TABLE stg.policy_cleaned IS
    'One row per policy-year, typed. Uniqueness of idpol is enforced by a cleaning rule and asserted by tests, not by a primary key; see the header of 002.';

DROP TABLE IF EXISTS stg.claim_cleaned;
CREATE TABLE stg.claim_cleaned (
    claim_seq    bigserial PRIMARY KEY,
    idpol        bigint  NOT NULL,
    claim_amount numeric NOT NULL
);

COMMENT ON TABLE stg.claim_cleaned IS
    'One row per claim. idpol repeats: 26,639 claims across 24,950 policies. Aggregation is deliberately not done here, so that the fact layer keeps claim grain and any join fan-out stays visible.';

CREATE INDEX claim_cleaned_idpol_idx ON stg.claim_cleaned (idpol);
