-- title: Claims for one policy
-- index: fact.claim_idpol_idx
-- why: point lookup on the referencing side of a foreign key
SELECT * FROM fact.claim WHERE idpol = 2241683;
