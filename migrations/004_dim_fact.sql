-- 004 · star schema: five dimensions, two facts, and the constraints staging
--        deliberately did not carry
--
-- Structure only; sql/transform/004_dim_fact.sql fills it. Staging had no
-- CHECK or foreign key constraints because its rules work by inserting
-- everything and deleting per rule. This layer is downstream of those rules, so
-- this is where the invariants become the database's job instead of a test's.
--
-- Grain.
--   fact.exposure  one row per policy-year           678,013
--   fact.claim     one row per claim, not per policy
--
-- fact.claim is not aggregated to policy level. Collapsing it here would hide
-- the fan-out: a policy with 16 claims joined to exposure yields 16 rows, and a
-- sum of exposure over that join counts the policy-year 16 times. Keeping claim
-- grain makes the fan-out visible and puts aggregation in the mart, where the
-- grain of the output is explicit.
--
-- Five dimensions, not the four the plan named. Area was going to be an
-- attribute of region. It is not: all 22 regions span several areas. Area is a
-- banding of density instead, so it gets its own dimension. See DEVLOG.

CREATE SCHEMA IF NOT EXISTS dim;
CREATE SCHEMA IF NOT EXISTS fact;

-- Dropped in dependency order, facts before the dimensions they reference.
DROP TABLE IF EXISTS fact.claim;
DROP TABLE IF EXISTS fact.exposure;
DROP TABLE IF EXISTS dim.region;
DROP TABLE IF EXISTS dim.area;
DROP TABLE IF EXISTS dim.vehicle;
DROP TABLE IF EXISTS dim.driver_band;
DROP TABLE IF EXISTS dim.bonus_band;

-- ---------------------------------------------------------------------------
-- Dimensions
-- ---------------------------------------------------------------------------

CREATE TABLE dim.region (
    region_key  smallint PRIMARY KEY,
    region_code text     NOT NULL UNIQUE
);

-- density_min and density_max describe the area; they cannot be used to derive
-- it. The upstream banding assigns the boundary densities 50, 100 and 500 to
-- both neighbouring areas, so the ranges overlap at exactly those values and
-- area_code has to be taken from the source rather than recomputed.
CREATE TABLE dim.area (
    area_key    smallint PRIMARY KEY,
    area_code   text     NOT NULL UNIQUE,
    density_min integer  NOT NULL,
    density_max integer  NOT NULL,
    CHECK (density_min <= density_max)
);

-- Vehicle type, not vehicle. veh_age is deliberately absent: a car ages from one
-- policy-year to the next, so age is a property of the policy-year and lives on
-- the fact table.
CREATE TABLE dim.vehicle (
    vehicle_key integer  PRIMARY KEY,
    veh_brand   text     NOT NULL,
    veh_gas     text     NOT NULL CHECK (veh_gas IN ('Regular', 'Diesel')),
    veh_power   smallint NOT NULL,
    UNIQUE (veh_brand, veh_gas, veh_power)
);

-- Reporting bands. They are not the modelling bands: D2-3 chooses those from
-- the data, and a rating plan is allowed to report on one banding and price on
-- another. These are conventional and fixed so that the mart and the Power BI
-- extract have stable categories that do not move every time a model is refit.
CREATE TABLE dim.driver_band (
    driver_band_key smallint PRIMARY KEY,
    label           text     NOT NULL UNIQUE,
    age_min         smallint NOT NULL,
    age_max         smallint NOT NULL,
    CHECK (age_min <= age_max),
    UNIQUE (age_min)
);

-- Anchored on the French bonus-malus scale rather than on quantiles. 100 is the
-- entry coefficient, each claim-free year takes 5% off to a floor of 50, and
-- each claim adds 25%. So 50, below 100, exactly 100 and above 100 are four
-- different states of a policy's history, not four slices of a distribution.
CREATE TABLE dim.bonus_band (
    bonus_band_key smallint PRIMARY KEY,
    label          text     NOT NULL UNIQUE,
    bm_min         smallint NOT NULL,
    bm_max         smallint NOT NULL,
    CHECK (bm_min <= bm_max),
    UNIQUE (bm_min)
);

-- ---------------------------------------------------------------------------
-- Facts
-- ---------------------------------------------------------------------------

CREATE TABLE fact.exposure (
    idpol               bigint   PRIMARY KEY,

    region_key          smallint NOT NULL REFERENCES dim.region,
    area_key            smallint NOT NULL REFERENCES dim.area,
    vehicle_key         integer  NOT NULL REFERENCES dim.vehicle,
    driver_band_key     smallint NOT NULL REFERENCES dim.driver_band,
    bonus_band_key      smallint NOT NULL REFERENCES dim.bonus_band,

    claim_nb            integer  NOT NULL CHECK (claim_nb >= 0),

    -- exposure is the modelling value, capped by transform 003. The uncapped
    -- value is kept beside it so the decision stays visible in the fact table.
    exposure            numeric  NOT NULL CHECK (exposure > 0 AND exposure <= 1),
    exposure_raw        numeric  NOT NULL CHECK (exposure_raw > 0),

    -- The continuous rating variables, unbanded. The band keys above serve
    -- reporting; modelling needs the raw values so it can choose its own bands.
    driv_age            smallint NOT NULL,
    veh_age             smallint NOT NULL,
    bonus_malus         smallint NOT NULL,
    density             integer  NOT NULL,

    is_exposure_capped  boolean  NOT NULL,
    is_high_claim_count boolean  NOT NULL,
    is_short_exposure   boolean  NOT NULL
);

CREATE TABLE fact.claim (
    claim_key    bigint  PRIMARY KEY,
    idpol        bigint  NOT NULL REFERENCES fact.exposure (idpol),
    claim_amount numeric NOT NULL CHECK (claim_amount > 0)
);

COMMENT ON TABLE fact.claim IS
    'Claim grain. idpol repeats. Aggregate in mart, never by joining this to fact.exposure and summing exposure.';
