-- title: Vehicle drilldown, region R24 (23.7% of policies)
-- index: fact.exposure_region_key_idx
-- why: the same index on an unselective filter, where it should not
SELECT vehicle_key, sum(exposure), sum(claim_nb)
FROM fact.exposure WHERE region_key = {region:R24} GROUP BY vehicle_key;
