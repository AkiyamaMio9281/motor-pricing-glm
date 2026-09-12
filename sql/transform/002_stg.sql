-- transform 002 · fill the staging tables, recording every rule
--
-- Re-runnable. Truncates first, so running it twice leaves the same state as
-- running it once, and it can be re-run after a fresh load without a migration
-- reset. Requires raw.freq_raw and raw.sev_raw to be loaded; scripts/transform.py
-- refuses to run against an empty raw layer rather than producing an audit
-- trail that reconciles while describing nothing.
--
-- Rules work by inserting everything and then deleting per rule. That is what
-- makes rows_out in stg.cleaning_audit reconcile from one rule to the next.

TRUNCATE stg.policy_cleaned;
TRUNCATE stg.claim_cleaned RESTART IDENTITY;
DELETE FROM stg.cleaning_audit
 WHERE table_name IN ('stg.policy_cleaned', 'stg.claim_cleaned');

-- ---------------------------------------------------------------------------
-- Policies
-- ---------------------------------------------------------------------------

DO $rules$
DECLARE
    v_affected bigint;
BEGIN
    -- Rule 1. Cast, routing the identifier through numeric rather than going
    -- straight to bigint.
    --
    -- One policy id in the upstream file is written as 1e+05. A direct cast of
    -- that text to bigint raises "invalid input syntax"; via numeric it
    -- resolves to 100000, which is not otherwise present, so no identifier
    -- collides. Had the raw layer been typed, this single row would have failed
    -- the COPY of all 678,013 and looked like a corrupt download.
    INSERT INTO stg.policy_cleaned
    SELECT
        (idpol::numeric)::bigint,
        claimnb::integer,
        exposure::numeric,
        area,
        vehpower::integer,
        vehage::integer,
        drivage::integer,
        bonusmalus::integer,
        vehbrand,
        vehgas,
        density::integer,
        region
    FROM raw.freq_raw;

    SELECT count(*) INTO v_affected
    FROM raw.freq_raw WHERE idpol !~ '^[0-9]+$';
    PERFORM stg.record_rule(
        1, 'cast_text_to_types', 'transform', 'stg.policy_cleaned', v_affected,
        'ids needing the numeric route; the remainder cast directly to bigint'
    );

    -- Rule 2. A policy-year must be identifiable exactly once, or none of the
    -- later per-policy rules mean anything.
    WITH d AS (
        DELETE FROM stg.policy_cleaned p
        WHERE EXISTS (
            SELECT 1 FROM stg.policy_cleaned q
            WHERE q.idpol = p.idpol AND q.ctid < p.ctid
        )
        RETURNING 1
    )
    SELECT count(*) INTO v_affected FROM d;
    PERFORM stg.record_rule(
        2, 'reject_duplicate_idpol', 'reject', 'stg.policy_cleaned', v_affected
    );

    -- Rule 3. Exposure is the offset denominator in the frequency model, so a
    -- zero or negative value is not a small error. It is a division by zero
    -- wearing a disguise.
    WITH d AS (
        DELETE FROM stg.policy_cleaned WHERE exposure <= 0 RETURNING 1
    )
    SELECT count(*) INTO v_affected FROM d;
    PERFORM stg.record_rule(
        3, 'reject_nonpositive_exposure', 'reject', 'stg.policy_cleaned',
        v_affected
    );

    -- Rule 4. A claim count cannot be negative.
    WITH d AS (
        DELETE FROM stg.policy_cleaned WHERE claim_nb < 0 RETURNING 1
    )
    SELECT count(*) INTO v_affected FROM d;
    PERFORM stg.record_rule(
        4, 'reject_negative_claim_nb', 'reject', 'stg.policy_cleaned',
        v_affected
    );

    -- Rule 5. Categorical levels outside the declared domains. An unexpected
    -- level does not raise anything. It becomes its own GLM coefficient fitted
    -- on a handful of rows, and then appears in the rate table as a factor with
    -- the same standing as the real ones.
    WITH d AS (
        DELETE FROM stg.policy_cleaned
        WHERE area      NOT IN ('A','B','C','D','E','F')
           OR veh_gas   NOT IN ('Regular','Diesel')
           OR veh_brand NOT IN ('B1','B2','B3','B4','B5','B6',
                                'B10','B11','B12','B13','B14')
           OR region    NOT IN ('R11','R21','R22','R23','R24','R25','R26',
                                'R31','R41','R42','R43','R52','R53','R54',
                                'R72','R73','R74','R82','R83','R91','R93','R94')
        RETURNING 1
    )
    SELECT count(*) INTO v_affected FROM d;
    PERFORM stg.record_rule(
        5, 'reject_unknown_category_level', 'reject', 'stg.policy_cleaned',
        v_affected
    );

    -- Rule 6. Observation only. 003 decides between truncating and dropping.
    SELECT count(*) INTO v_affected
    FROM stg.policy_cleaned WHERE exposure > 1;
    PERFORM stg.record_rule(
        6, 'observe_exposure_above_one', 'observe', 'stg.policy_cleaned',
        v_affected,
        'policy-years longer than one year; decision deferred to 003'
    );

    -- Rule 7. Observation only, and the consequential one. A policy reporting
    -- claims with no matching amount in the severity file cannot contribute to
    -- the severity model, so frequency and severity end up fitted on different
    -- effective populations. Restricting severity to claim_nb > 0, which is the
    -- usual advice, does not avoid this.
    SELECT count(*) INTO v_affected
    FROM stg.policy_cleaned p
    WHERE p.claim_nb > 0
      AND NOT EXISTS (
          SELECT 1 FROM raw.sev_raw s
          WHERE (s.idpol::numeric)::bigint = p.idpol
      );
    PERFORM stg.record_rule(
        7, 'observe_claims_without_amount', 'observe', 'stg.policy_cleaned',
        v_affected,
        'policies reporting claims that the severity file does not price'
    );
END;
$rules$;

-- ---------------------------------------------------------------------------
-- Claims
-- ---------------------------------------------------------------------------

DO $rules$
DECLARE
    v_affected bigint;
BEGIN
    INSERT INTO stg.claim_cleaned (idpol, claim_amount)
    SELECT (idpol::numeric)::bigint, claimamount::numeric FROM raw.sev_raw;

    SELECT count(*) INTO v_affected
    FROM raw.sev_raw WHERE idpol !~ '^[0-9]+$';
    PERFORM stg.record_rule(
        1, 'cast_text_to_types', 'transform', 'stg.claim_cleaned', v_affected
    );

    -- A Gamma severity model cannot take a non-positive amount, and a zero
    -- amount is a claim closed without payment rather than a priced one.
    WITH d AS (
        DELETE FROM stg.claim_cleaned WHERE claim_amount <= 0 RETURNING 1
    )
    SELECT count(*) INTO v_affected FROM d;
    PERFORM stg.record_rule(
        2, 'reject_nonpositive_claim_amount', 'reject', 'stg.claim_cleaned',
        v_affected
    );

    -- Observation only. These are claims whose policy-year is absent from the
    -- frequency file, so there is no exposure to price them against. They are
    -- removed in 004 when the fact layer takes its foreign key, not here,
    -- because the reason is referential rather than a property of the row.
    SELECT count(*) INTO v_affected
    FROM stg.claim_cleaned c
    WHERE NOT EXISTS (
        SELECT 1 FROM stg.policy_cleaned p WHERE p.idpol = c.idpol
    );
    PERFORM stg.record_rule(
        3, 'observe_orphan_claims', 'observe', 'stg.claim_cleaned', v_affected,
        'claims whose policy-year is not in the frequency file'
    );
END;
$rules$;
