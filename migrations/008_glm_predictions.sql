-- 008 · recorded losses per policy, and the GLM output R hands to Python

CREATE VIEW model.policy_loss AS
SELECT
    e.idpol,
    count(c.claim_key)::integer                         AS priced_claim_nb,
    coalesce(sum(c.claim_amount), 0)::double precision  AS incurred_loss
FROM fact.exposure e
LEFT JOIN fact.claim c USING (idpol)
GROUP BY e.idpol;

CREATE TABLE model.glm_run (
    run                 text             PRIMARY KEY,
    frame_md5           text             NOT NULL CHECK (frame_md5 ~ '^[0-9a-f]{32}$'),
    frequency_response  text             NOT NULL CHECK (frequency_response IN ('claim_nb', 'priced_claim_nb')),
    large_loss_cap      double precision NOT NULL CHECK (large_loss_cap > 0),
    large_loss_load     double precision NOT NULL CHECK (large_loss_load >= 1)
);

CREATE TABLE model.glm_prediction (
    run                      text             NOT NULL REFERENCES model.glm_run ON DELETE CASCADE,
    idpol                    bigint           NOT NULL,
    claims_per_year          double precision NOT NULL CHECK (claims_per_year > 0),
    capped_amount_per_claim  double precision NOT NULL CHECK (capped_amount_per_claim > 0),
    PRIMARY KEY (run, idpol)
);

CREATE TABLE model.glm_coefficient (
    run       text             NOT NULL REFERENCES model.glm_run ON DELETE CASCADE,
    model     text             NOT NULL CHECK (model IN ('frequency', 'severity')),
    term      text             NOT NULL,
    estimate  double precision NOT NULL,
    PRIMARY KEY (run, model, term)
);
