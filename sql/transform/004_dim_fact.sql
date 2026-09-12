-- transform 004 · fill the star schema
--
-- Re-runnable, and atomic: scripts/transform.py runs each file in one
-- transaction, so a constraint violation part-way down leaves every table below
-- exactly as it was. Verified on the first run of this file, which failed on the
-- foreign key into fact.exposure and left all seven tables empty.
--
-- The dimension joins are LEFT JOINs into NOT NULL keys, on purpose. An INNER
-- JOIN to a band with a gap in its ranges does not fail. It drops the policy,
-- and the fact table comes out a few thousand rows short with no error. A LEFT
-- JOIN turns the same gap into a null key, and the NOT NULL on the fact table
-- turns the null into a failed transform. An overlap between two bands is
-- caught the other way round: the policy matches twice and the primary key on
-- idpol refuses the duplicate.

TRUNCATE fact.claim, fact.exposure,
         dim.region, dim.area, dim.vehicle, dim.driver_band, dim.bonus_band;
DELETE FROM stg.cleaning_audit WHERE table_name IN ('fact.exposure', 'fact.claim');

-- ---------------------------------------------------------------------------
-- Dimensions
-- ---------------------------------------------------------------------------

INSERT INTO dim.region (region_key, region_code)
SELECT row_number() OVER (ORDER BY region), region
FROM (SELECT DISTINCT region FROM stg.policy_cleaned) r;

INSERT INTO dim.area (area_key, area_code, density_min, density_max)
SELECT row_number() OVER (ORDER BY area), area, min(density), max(density)
FROM stg.policy_cleaned
GROUP BY area;

INSERT INTO dim.vehicle (vehicle_key, veh_brand, veh_gas, veh_power)
SELECT row_number() OVER (ORDER BY veh_brand, veh_gas, veh_power),
       veh_brand, veh_gas, veh_power
FROM (SELECT DISTINCT veh_brand, veh_gas, veh_power FROM stg.policy_cleaned) v;

-- Conventional motor bands, narrow where risk changes fastest. Upper bound 100
-- is the observed maximum driver age; a later policy with a driver of 101
-- would fail this transform rather than vanish from it.
INSERT INTO dim.driver_band (driver_band_key, label, age_min, age_max) VALUES
    (1, '18-20',  18,  20),
    (2, '21-25',  21,  25),
    (3, '26-30',  26,  30),
    (4, '31-40',  31,  40),
    (5, '41-50',  41,  50),
    (6, '51-60',  51,  60),
    (7, '61-70',  61,  70),
    (8, '71+',    71, 100);

-- The four states of the French bonus-malus scale. See migration 004.
INSERT INTO dim.bonus_band (bonus_band_key, label, bm_min, bm_max) VALUES
    (1, '50 maximum bonus',  50,  50),
    (2, '51-99 bonus',       51,  99),
    (3, '100 entry',        100, 100),
    (4, '101+ malus',       101, 230);

-- ---------------------------------------------------------------------------
-- fact.exposure
-- ---------------------------------------------------------------------------

INSERT INTO fact.exposure
SELECT
    c.idpol,
    r.region_key, a.area_key, v.vehicle_key,
    db.driver_band_key, bb.bonus_band_key,
    c.claim_nb,
    p.exposure_adj, c.exposure,
    c.driv_age, c.veh_age, c.bonus_malus, c.density,
    p.is_exposure_capped, p.is_high_claim_count, p.is_short_exposure
FROM stg.policy_cleaned c
JOIN stg.policy_adjusted p USING (idpol)
LEFT JOIN dim.region  r ON r.region_code = c.region
LEFT JOIN dim.area    a ON a.area_code   = c.area
LEFT JOIN dim.vehicle v ON (v.veh_brand, v.veh_gas, v.veh_power)
                         = (c.veh_brand, c.veh_gas, c.veh_power)
LEFT JOIN dim.driver_band db ON c.driv_age    BETWEEN db.age_min AND db.age_max
LEFT JOIN dim.bonus_band  bb ON c.bonus_malus BETWEEN bb.bm_min  AND bb.bm_max;

DO $rules$
DECLARE
    v_staged bigint;
    v_fact   bigint;
BEGIN
    SELECT count(*) INTO v_staged FROM stg.policy_cleaned;
    SELECT count(*) INTO v_fact   FROM fact.exposure;

    -- The LEFT JOINs make a silent drop impossible, but a join that fanned out
    -- would still be caught only by the primary key, and only if it duplicated
    -- an idpol. Asserting the count closes the remaining gap.
    IF v_fact <> v_staged THEN
        RAISE EXCEPTION
            'fact.exposure has % rows but stg.policy_cleaned has %',
            v_fact, v_staged;
    END IF;

    PERFORM stg.record_rule(
        1, 'load_policy_years', 'transform', 'fact.exposure', v_fact,
        'one row per policy-year; count asserted equal to stg.policy_cleaned'
    );
END;
$rules$;

-- ---------------------------------------------------------------------------
-- fact.claim
-- ---------------------------------------------------------------------------

-- The first version of this insert took every staged claim, and the foreign key
-- refused it:
--
--   ERROR: insert or update on table "claim" violates foreign key constraint
--          "claim_idpol_fkey"
--   DETAIL: Key (idpol)=(2262511) is not present in table "exposure".
--
-- The message names one key and stops. The violation is 195 rows across six
-- policies, carrying 21 to 66 claims each, and that scope was already on record
-- as observe_orphan_claims in 002. The foreign key did not discover the
-- problem; it enforced a decision that had been deferred to this layer.
--
-- The decision is to exclude them. A claim with no policy-year has no exposure
-- to be priced against, so it cannot enter a frequency model, and in a severity
-- model it would contribute amounts from policies whose rating factors are
-- unknown. There is nothing to impute them from.
INSERT INTO fact.claim (claim_key, idpol, claim_amount)
SELECT c.claim_seq, c.idpol, c.claim_amount
FROM stg.claim_cleaned c
WHERE EXISTS (SELECT 1 FROM fact.exposure e WHERE e.idpol = c.idpol);

DO $rules$
DECLARE
    v_staged   bigint;
    v_loaded   bigint;
    v_excluded bigint;
    v_policies bigint;
    v_amount   numeric;
BEGIN
    SELECT count(*) INTO v_staged FROM stg.claim_cleaned;
    SELECT count(*) INTO v_loaded FROM fact.claim;

    SELECT count(*), count(DISTINCT idpol), coalesce(sum(claim_amount), 0)
    INTO v_excluded, v_policies, v_amount
    FROM stg.claim_cleaned c
    WHERE NOT EXISTS (SELECT 1 FROM fact.exposure e WHERE e.idpol = c.idpol);

    IF v_loaded + v_excluded <> v_staged THEN
        RAISE EXCEPTION
            'fact.claim % plus excluded % does not equal staged %',
            v_loaded, v_excluded, v_staged;
    END IF;

    -- record_rule counts rows_out from the table, so for a reject it has to be
    -- called on the loaded table with the number excluded, which is what makes
    -- the chain read staged -> excluded -> loaded.
    PERFORM stg.record_rule(
        1, 'reject_claims_without_policy_year', 'reject', 'fact.claim',
        v_excluded,
        format('%s claims across %s policies, total amount %s; no exposure to price them against',
               v_excluded, v_policies, round(v_amount))
    );
END;
$rules$;
