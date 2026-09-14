-- 007 · the severity model frame: one row per priced claim
--
-- Severity is modelled on claims that carry an amount, which are exactly the rows
-- of fact.claim, and on nothing else.
--
-- The rating factors are taken from model.frequency_frame by joining on idpol,
-- not re-derived from the fact and dimension tables. Frequency and severity then
-- see the same area, region, vehicle and age values for the same policy by
-- construction, because there is only one place those values are assembled.
--
-- What this frame does not contain is as important as what it does.
--
-- Zero-claim policies. A Gamma model cannot take a zero, and whether a zero-claim
-- policy makes a fit fail or quietly disappears from it depends on whether its
-- missing amount was written as 0 or left null. Built from fact.claim, the frame
-- has no row for such a policy to begin with.
--
-- The 9,116 policies that report claims the severity file never prices. Filtering
-- a policy-level frame to claim_nb > 0 does not remove them; they either break the
-- fit or vanish from it silently. They are absent here for the same reason as the
-- zero-claim policies, and docs/severity-population.md measures what their absence
-- does to pure premium.
--
-- claim_amount is cast to float8 for the reason migration 006 casts exposure:
-- psycopg would otherwise hand Python a column of Decimal objects.

CREATE OR REPLACE VIEW model.severity_frame AS
SELECT
    c.claim_key,
    c.claim_amount::double precision AS claim_amount,
    f.*
FROM fact.claim c
JOIN model.frequency_frame f USING (idpol);

COMMENT ON VIEW model.severity_frame IS
    'Claim grain, priced claims only. Rating factors come from model.frequency_frame. Contains no zero-claim policy and no unpriced claim.';
