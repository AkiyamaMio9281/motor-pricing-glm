-- 006 · the frequency model frame: one view that R and Python both read
--
-- Modelling happens in two languages, R for the GLMs and Python for the
-- benchmark and validation. If each assembled its own frame from the fact
-- tables, there would be two definitions of what a policy-year looks like to a
-- model, and the two tracks' numbers would disagree for reasons that have
-- nothing to do with modelling. This view is the single definition. The R
-- loader and the Python loader do no joins of their own, and a test checks that
-- they receive byte-identical frames.
--
-- One row per policy-year. Severity has a different grain and a different set
-- of decisions to make, and gets its own frame in D2-5.

CREATE SCHEMA IF NOT EXISTS model;

-- Rating factors appear as codes, never as surrogate keys.
--
-- region_key is an integer from 1 to 22. Passed to a model formula, an integer
-- column is a continuous covariate: one slope across regions whose numbering is
-- alphabetical and means nothing. region as text becomes a factor with 21
-- contrasts. model.matrix(~ region_key) has 2 columns and model.matrix(~ region)
-- has 22, and neither call warns. Leaving the keys out of the frame makes the
-- wrong one impossible rather than merely discouraged.
--
-- exposure is cast to double precision. The fact table keeps it as numeric for
-- exact storage, but the two clients read numeric differently: RPostgres returns
-- a double, psycopg returns Python Decimal objects, which pandas stores as an
-- object column that is slow and that numpy will not do arithmetic on without a
-- conversion each language would then have to remember. Casting here hands both
-- the same float, and Postgres emits float8 as the shortest text that
-- round-trips, so both parse it to the identical double.
--
-- The joins are INNER on purpose, and safe here in a way they were not in
-- transform 004. Every key is NOT NULL with a foreign key into its dimension, so
-- an inner join cannot drop a row; the loaders and a test assert the row count
-- all the same.
--
-- The uncapped exposure is not in the frame. It would sit one column away from
-- the modelling exposure with a name that invites offset(log(exposure_raw)); it
-- stays in fact.exposure for anyone who needs it deliberately. The flags stay in,
-- because D2 refits with and without the rows they mark.
--
-- Factor levels and base levels are deliberately not decided here. Base level
-- changes the interpretation of coefficients and not the predictions, so it is
-- chosen where interpretation matters, in the rate table (D4-2).

CREATE OR REPLACE VIEW model.frequency_frame AS
SELECT
    e.idpol,
    e.claim_nb,
    e.exposure::double precision AS exposure,
    a.area_code                  AS area,
    v.veh_power,
    e.veh_age,
    e.driv_age,
    e.bonus_malus,
    v.veh_brand,
    v.veh_gas,
    e.density,
    r.region_code                AS region,
    e.is_short_exposure,
    e.is_high_claim_count,
    e.is_exposure_capped
FROM fact.exposure e
JOIN dim.region  r USING (region_key)
JOIN dim.area    a USING (area_key)
JOIN dim.vehicle v USING (vehicle_key);

COMMENT ON VIEW model.frequency_frame IS
    'Policy-year grain modelling frame. Codes not keys; exposure as float8. Read by R/model_frame.R and scripts/frame.py, which must agree row for row.';
