-- 010 · the LightGBM benchmark's predictions

CREATE TABLE model.benchmark_run (
    run              text             PRIMARY KEY,
    frame_md5        text             NOT NULL CHECK (frame_md5 ~ '^[0-9a-f]{32}$'),
    trained_on       text             NOT NULL CHECK (trained_on IN ('all', 'risk_group_split', 'idpol_split')),
    large_loss_cap   double precision NOT NULL CHECK (large_loss_cap > 0),
    large_loss_load  double precision NOT NULL CHECK (large_loss_load >= 1),
    variance_power   double precision NOT NULL CHECK (variance_power > 1 AND variance_power < 2),
    boosting_rounds  integer          NOT NULL CHECK (boosting_rounds > 0),
    balance_factor   double precision NOT NULL CHECK (balance_factor > 0),
    parameters       jsonb            NOT NULL
);

CREATE TABLE model.benchmark_prediction (
    run                     text             NOT NULL REFERENCES model.benchmark_run ON DELETE CASCADE,
    idpol                   bigint           NOT NULL,
    capped_amount_per_year  double precision NOT NULL CHECK (capped_amount_per_year > 0),
    PRIMARY KEY (run, idpol)
);
