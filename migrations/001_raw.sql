-- 001 · raw landing zone for the freMTPL2 extracts
--
-- Every data column is text on purpose.
--
-- A COPY into this layer must not be able to fail on a type error. That keeps a
-- broken load and a broken cleaning rule as two distinguishable events: if 001
-- succeeds and 002 rejects rows, the problem is in the data and the audit trail
-- in stg.cleaning_audit will name the rule. If the text load itself fails, the
-- problem is in transport -- a truncated download, a wrong delimiter, a shifted
-- header. Casting in the raw layer would collapse those two cases into one
-- error message.
--
-- Consequence to remember: nothing in this schema may be read by a model. The
-- only legitimate consumer of raw.* is 002_stg.sql.

CREATE SCHEMA IF NOT EXISTS raw;

DROP TABLE IF EXISTS raw.freq_raw;
CREATE TABLE raw.freq_raw (
    idpol       text,
    claimnb     text,
    exposure    text,
    area        text,
    vehpower    text,
    vehage      text,
    drivage     text,
    bonusmalus  text,
    vehbrand    text,
    vehgas      text,
    density     text,
    region      text
);

COMMENT ON TABLE raw.freq_raw IS
    'freMTPL2freq as delivered, one row per policy-year. All text; cast in stg.';

DROP TABLE IF EXISTS raw.sev_raw;
CREATE TABLE raw.sev_raw (
    idpol       text,
    claimamount text
);

COMMENT ON TABLE raw.sev_raw IS
    'freMTPL2sev as delivered, one row per individual claim. Repeats idpol.';

-- Provenance is recorded per load, not per row. A _src_file column on 678k rows
-- would cost more than it explains, and the questions actually worth answering
-- later are "which file, which checksum, how many rows, how long" -- one row per
-- load answers all four. The duration column is what D1-2 compares when the
-- loader switches off pandas.to_sql.
DROP TABLE IF EXISTS raw.load_audit;
CREATE TABLE raw.load_audit (
    load_id     bigserial PRIMARY KEY,
    table_name  text        NOT NULL,
    src_file    text        NOT NULL,
    src_sha256  text        NOT NULL,
    method      text        NOT NULL,
    row_count   bigint      NOT NULL,
    duration_ms integer     NOT NULL,
    loaded_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE raw.load_audit IS
    'One row per load attempt. Feeds the loader timing comparison in DEVLOG.';
