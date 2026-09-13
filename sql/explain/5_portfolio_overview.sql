-- title: Portfolio overview by driver band and bonus band
-- index: none
-- why: reads every policy-year by definition; no index applies
SELECT driver_band_key, bonus_band_key, sum(exposure), sum(claim_nb)
FROM fact.exposure GROUP BY driver_band_key, bonus_band_key;
