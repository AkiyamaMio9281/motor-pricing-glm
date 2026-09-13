-- title: Delete the claim-free policy-years of one region
-- index: fact.claim_idpol_idx
-- why: each deleted policy-year must be checked against fact.claim
DELETE FROM fact.exposure WHERE region_key = {region:R43} AND claim_nb = 0;
