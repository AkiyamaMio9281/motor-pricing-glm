-- 011 · the modelling bands as reference data, and experience by segment

CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE mart.rating_band (
    factor       text    NOT NULL CHECK (factor IN ('driv_age', 'veh_age', 'bonus_malus')),
    lower_bound  integer NOT NULL,
    label        text    NOT NULL,
    PRIMARY KEY (factor, lower_bound),
    UNIQUE (factor, label)
);

INSERT INTO mart.rating_band (factor, lower_bound, label) VALUES
    ('driv_age', 18, '18-19'), ('driv_age', 20, '20-21'), ('driv_age', 22, '22-24'), ('driv_age', 25, '25-29'),
    ('driv_age', 30, '30-34'), ('driv_age', 35, '35-39'), ('driv_age', 40, '40-44'), ('driv_age', 45, '45-49'),
    ('driv_age', 50, '50-54'), ('driv_age', 55, '55-59'), ('driv_age', 60, '60-64'), ('driv_age', 65, '65-69'),
    ('driv_age', 70, '70-79'), ('driv_age', 80, '80+'),
    ('veh_age', 0, '0'), ('veh_age', 1, '1'), ('veh_age', 2, '2'), ('veh_age', 3, '3-4'), ('veh_age', 5, '5-6'),
    ('veh_age', 7, '7-9'), ('veh_age', 10, '10-11'), ('veh_age', 12, '12-14'), ('veh_age', 15, '15-19'),
    ('veh_age', 20, '20+'),
    ('bonus_malus', 50, '50'), ('bonus_malus', 51, '51-54'), ('bonus_malus', 55, '55-59'), ('bonus_malus', 60, '60-69'),
    ('bonus_malus', 70, '70-79'), ('bonus_malus', 80, '80-89'), ('bonus_malus', 90, '90-99'),
    ('bonus_malus', 100, '100'), ('bonus_malus', 101, '101-119'), ('bonus_malus', 120, '120+');

CREATE VIEW mart.policy_segment AS
WITH bands AS (
    SELECT factor, label, lower_bound,
           lead(lower_bound) OVER (PARTITION BY factor ORDER BY lower_bound) AS upper_bound
    FROM mart.rating_band
)
SELECT
    f.idpol,
    f.exposure,
    f.claim_nb,
    l.priced_claim_nb,
    l.incurred_loss,
    f.area,
    f.region,
    f.veh_brand,
    f.veh_gas,
    f.veh_power,
    f.density,
    d.label AS driv_age_band,
    v.label AS veh_age_band,
    b.label AS bonus_malus_band
FROM model.frequency_frame f
JOIN model.policy_loss l USING (idpol)
LEFT JOIN bands d ON d.factor = 'driv_age' AND f.driv_age >= d.lower_bound
                 AND (d.upper_bound IS NULL OR f.driv_age < d.upper_bound)
LEFT JOIN bands v ON v.factor = 'veh_age' AND f.veh_age >= v.lower_bound
                 AND (v.upper_bound IS NULL OR f.veh_age < v.upper_bound)
LEFT JOIN bands b ON b.factor = 'bonus_malus' AND f.bonus_malus >= b.lower_bound
                 AND (b.upper_bound IS NULL OR f.bonus_malus < b.upper_bound);

CREATE VIEW mart.experience_by_segment AS
SELECT
    region,
    driv_age_band,
    count(*)                                              AS policy_years,
    sum(exposure)                                         AS exposure,
    sum(claim_nb)                                         AS reported_claims,
    sum(priced_claim_nb)                                  AS priced_claims,
    sum(incurred_loss)                                    AS incurred_loss,
    sum(priced_claim_nb) / sum(exposure)                  AS claim_frequency,
    sum(incurred_loss) / nullif(sum(priced_claim_nb), 0)  AS severity,
    sum(incurred_loss) / sum(exposure)                    AS loss_per_policy_year
FROM mart.policy_segment
GROUP BY region, driv_age_band;
