-- title: Vehicle drilldown, region R43 (0.2% of policies)
-- index: fact.exposure_region_key_idx
-- why: a selective filter, where an index should pay
SELECT vehicle_key, sum(exposure), sum(claim_nb)
FROM fact.exposure WHERE region_key = {region:R43} GROUP BY vehicle_key;
