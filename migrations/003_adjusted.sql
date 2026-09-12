-- 003 · structure for the pricing decisions
--
-- 002 removes rows that are malformed. This layer touches rows that are fine as
-- data and questionable as evidence, which is a different kind of act and gets
-- a different table.
--
-- Nothing here overwrites anything. stg.policy_cleaned keeps what the file
-- said; this table carries what the project decided to model on, beside it, one
-- row per policy. A reviewer who disagrees with a decision can see both values
-- and the flag that separates them, and rebuilding with a different threshold
-- is re-running one transform rather than re-deriving the mart.
--
-- Deliberately narrow. The rating factors stay in stg.policy_cleaned and are
-- joined back by the model frame in 004; duplicating twelve columns across
-- 678,013 rows to save one join would make two tables that can disagree.

CREATE SCHEMA IF NOT EXISTS stg;

DROP TABLE IF EXISTS stg.policy_adjusted;
CREATE TABLE stg.policy_adjusted (
    idpol              bigint  NOT NULL,

    -- The modelling exposure. Capped at one policy-year; the uncapped value
    -- stays in stg.policy_cleaned.exposure.
    exposure_adj       numeric NOT NULL,
    is_exposure_capped boolean NOT NULL,

    -- claim_nb is not adjusted. The flag marks policies whose count is extreme
    -- enough to be worth naming in a diagnostic, not rows to be treated
    -- differently by the model.
    is_high_claim_count boolean NOT NULL,

    -- Exposure short enough that the proportionality the Poisson offset assumes
    -- is not observed in this portfolio. See the transform for the measurement
    -- this threshold comes from.
    is_short_exposure  boolean NOT NULL,

    -- claim_nb / exposure_adj, carried so that diagnostics and the D2 residual
    -- plots do not each recompute it with their own idea of which exposure.
    implied_rate       numeric NOT NULL
);

COMMENT ON TABLE stg.policy_adjusted IS
    'Pricing decisions, one row per policy, beside stg.policy_cleaned rather than over it. Flags name rows; they do not remove them.';

CREATE INDEX policy_adjusted_idpol_idx ON stg.policy_adjusted (idpol);
