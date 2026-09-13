-- transform 003 · apply the pricing decisions, and record what each one costs
--
-- Re-runnable. Every rule writes to stg.cleaning_audit like the cleaning rules
-- do, but nothing here rejects a row: the decisions are a cap and three flags,
-- so rows_out is 678,013 throughout and the interesting column is the count of
-- rows each decision touches.
--
-- Thresholds are stated here with the measurement that produced them, so that
-- changing one is an argument about evidence rather than about taste.

TRUNCATE stg.policy_adjusted;
DELETE FROM stg.cleaning_audit WHERE table_name = 'stg.policy_adjusted';

DO $rules$
DECLARE
    v_affected bigint;
BEGIN
    -- Decision 1. Cap exposure at one policy-year.
    --
    -- 1,224 policy-years run longer than a year, to a maximum of 2.01. The
    -- alternative is to drop them, and the two cost very different amounts:
    --
    --   capping  loses    139 exposure-years and no claims
    --   dropping loses  1,363 exposure-years and 54 claims
    --
    -- Against a portfolio of 358,499 exposure-years, capping costs 0.04% of
    -- exposure and nothing else. Dropping throws away a whole risk-year of
    -- observed claims to fix a bookkeeping artefact, and it would bias the
    -- portfolio frequency upward, because the over-one band has the lowest
    -- frequency in the book (0.04 against 0.10 overall).
    --
    -- A policy-year longer than a year is a record-keeping artefact, not a
    -- policy that was somehow exposed for longer than it existed. Capping
    -- treats it as such.
    INSERT INTO stg.policy_adjusted (
        idpol, exposure_adj, is_exposure_capped,
        is_high_claim_count, is_short_exposure, implied_rate
    )
    SELECT
        idpol,
        LEAST(exposure, 1),
        exposure > 1,
        claim_nb > 4,
        exposure < 0.02,
        claim_nb / LEAST(exposure, 1)
    FROM stg.policy_cleaned;

    SELECT count(*) INTO v_affected
    FROM stg.policy_adjusted WHERE is_exposure_capped;
    PERFORM stg.record_rule(
        1, 'cap_exposure_at_one', 'transform', 'stg.policy_adjusted',
        v_affected,
        'capping costs 139 exposure-years and no claims; dropping would cost 1,363 and 54 claims'
    );

    -- Decision 2. Flag, do not cap, an extreme claim count.
    --
    -- Nine policies report more than four claims, to a maximum of sixteen.
    -- Capping the count, which some treatments of this dataset do, would
    -- discard observed claims to make a histogram tidier. These are 0.001% of
    -- policies and the frequency model has an offset that already accounts for
    -- how long each was exposed.
    --
    -- The flag exists so the D2 diagnostics can name them, not so anything
    -- treats them differently.
    SELECT count(*) INTO v_affected
    FROM stg.policy_adjusted WHERE is_high_claim_count;
    PERFORM stg.record_rule(
        2, 'flag_high_claim_count', 'transform', 'stg.policy_adjusted',
        v_affected, 'claim_nb > 4; counts are flagged, never capped'
    );

    -- Decision 3. Flag exposure short enough that the offset's assumption is
    -- measurably false.
    --
    -- log(exposure) as an offset asserts that expected claims are proportional
    -- to exposure. Measured by band, in this portfolio, they are not:
    --
    --   exposure         policies   exposure-yrs   claims   claims per yr
    --   under a week       13,603            101      362            3.58
    --   under 5 weeks     107,950          6,825    2,793            0.41
    --   under half a yr   220,064         63,179    9,815            0.16
    --   half to one yr    335,172        287,031   23,078            0.08
    --   over one year       1,224          1,363       54            0.04
    --
    -- Under proportionality that last column would be flat at the portfolio
    -- rate of 0.10. Instead it spans a factor of ninety, monotonically. The
    -- probability of having any claim tells the same story: a policy exposed
    -- under a week has a 2.5% chance of a claim against 6.5% for a full year,
    -- where proportionality predicts nearer 0.06%.
    --
    -- The likely mechanism is not measurement error but selection: a policy
    -- that has a claim is more likely to end soon after, through cancellation,
    -- a total loss, or a change of insurer, so exposure is partly determined by
    -- the claim rather than preceding it. That is not something a cleaning rule
    -- can fix, and it is not a reason to delete the rows either. 13,603
    -- policies carry 362 claims, 1% of all claims, on 0.03% of exposure.
    --
    -- This comment originally called that the shape of a high-leverage point.
    -- D2-2 measured it and it is not: removing these rows moves no key
    -- frequency relativity by more than half a percent. Under an offset a row's
    -- working weight in the Poisson fit is proportional to its exposure, so a
    -- one-week policy carries a fiftieth of a full year's weight. The rows are
    -- high-residual, and they inflate the Pearson dispersion from 1.95 to 2.65,
    -- but they are low-influence. See docs/frequency-exposure.md.
    --
    -- The threshold is one week, chosen because it is the band where the
    -- departure is most extreme. D2-2 found the departure is not confined to
    -- it: the estimated coefficient on log(exposure) is 0.37 on all policy-years
    -- and still 0.40 with these rows removed. The flag remains useful for
    -- naming the rows that dominate the residuals.
    SELECT count(*) INTO v_affected
    FROM stg.policy_adjusted WHERE is_short_exposure;
    PERFORM stg.record_rule(
        3, 'flag_short_exposure', 'transform', 'stg.policy_adjusted',
        v_affected,
        'exposure < 0.02 yr; band frequency 3.58 against 0.10 portfolio-wide'
    );

    -- Decision 4. Observation. The implied rate is what the two above interact
    -- to produce, and it is recorded so that the worst cases are named rather
    -- than discovered during a model fit.
    SELECT count(*) INTO v_affected
    FROM stg.policy_adjusted WHERE implied_rate > 12;
    PERFORM stg.record_rule(
        4, 'observe_implied_rate_above_monthly', 'observe',
        'stg.policy_adjusted', v_affected,
        'policies implying more than one claim a month; max is 732 a year'
    );
END;
$rules$;
