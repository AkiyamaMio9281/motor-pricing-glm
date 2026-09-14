-- 009 · the two holdout splits, and the rows each GLM run was fitted on

CREATE VIEW model.holdout AS
WITH pieces AS (
    SELECT
        idpol,
        (area, veh_power, veh_age, driv_age, bonus_malus, veh_brand, veh_gas, density, region)
            IS DISTINCT FROM
        (lag(area) OVER w, lag(veh_power) OVER w, lag(veh_age) OVER w, lag(driv_age) OVER w,
         lag(bonus_malus) OVER w, lag(veh_brand) OVER w, lag(veh_gas) OVER w, lag(density) OVER w,
         lag(region) OVER w) AS starts_risk_group
    FROM model.frequency_frame
    WINDOW w AS (ORDER BY idpol)
),
grouped AS (
    SELECT idpol, sum(starts_risk_group::integer) OVER (ORDER BY idpol) AS risk_group
    FROM pieces
)
SELECT
    idpol,
    risk_group,
    risk_group % 5 = 0                                                AS risk_group_holdout,
    ('x' || substr(md5(idpol::text), 1, 8))::bit(32)::bigint % 5 = 0   AS idpol_holdout
FROM grouped;

ALTER TABLE model.glm_run
    ADD COLUMN trained_on text NOT NULL DEFAULT 'all'
    CHECK (trained_on IN ('all', 'risk_group_split', 'idpol_split'));

ALTER TABLE model.glm_run ALTER COLUMN trained_on DROP DEFAULT;
