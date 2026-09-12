# motor-pricing-glm

Motor third-party-liability pricing on the freMTPL2 portfolio: a SQL data mart,
frequency and severity GLMs in R, a pure-premium comparison against a gradient
boosting baseline in Python, and a credibility-weighted rate table exported for
actuarial review.

Work in progress. This README grows into the one-page summary as the layers
land; for now it is the setup path.

## Layers

| Layer | What | Language | State |
|---|---|---|---|
| L1 | Data mart: raw to staging to star schema to segment mart | SQL (Postgres) | raw and staging in |
| L2 | Claim frequency, Poisson GLM with exposure offset | R | |
| L3 | Claim severity, Gamma GLM on claiming policies only | R | |
| L4 | Pure premium, gradient boosting baseline, validation | Python | |
| L5 | Rate relativities, normalization, credibility weighting | Python | |
| L6 | Excel rate workbook and Power BI report | Python, Power BI | |

## Setup

Requires Docker, Python 3.14, and R 4.6.

```bash
cp .env.example .env          # then set POSTGRES_PASSWORD
docker compose up -d          # Postgres 16 on the port in .env

python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
Rscript -e 'install.packages(c("DBI","RPostgres","dplyr","ggplot2"), type="binary")'
```

Then build the mart, in this order:

```bash
.venv/Scripts/python scripts/migrate.py       # structure
.venv/Scripts/python scripts/fetch_data.py    # download, verify, convert
.venv/Scripts/python scripts/load_raw.py all  # COPY into the raw layer
.venv/Scripts/python scripts/transform.py     # build staging from raw
```

The order matters and is enforced rather than documented. Migrations change
structure and are safe against an empty database; transforms read one layer and
rewrite the next, and `transform.py` exits non-zero rather than building a layer
out of an empty source. See `DEVLOG.md` for what happened before it did.

Every cleaning rule writes its own row counts to `stg.cleaning_audit`, so
the path from 678,013 raw rows to the staged table is reconciled rule by rule
rather than asserted. `SELECT * FROM stg.cleaning_audit ORDER BY table_name,
rule_seq` prints the chain; `RESULTS.md` carries the current values.

`scripts/migrate.py --status` reports what is applied. `--reset` drops the
project schemas and rebuilds them from the SQL files, which is the intended way
to get back to a known state.

There are two Python 3.14 installations on the author's machine with separate
site-packages, so every script and every command in this repo names
`.venv/Scripts/python` explicitly rather than relying on whichever `python`
the shell resolves. See `DEVLOG.md`.

## Data

`data/` is not committed. freMTPL2 is public: 678,013 policy-years in the
frequency table and 26,639 individual claims in the severity table.

`fetch_data.py` pins the upstream MD5s rather than reading them back from the
same API it just downloaded from, so a replaced file upstream is detected rather
than confirmed. It writes `data/CHECKSUMS.txt` for the converted CSVs.

`load_raw.py --method executemany` is the slow path, kept so the loader
comparison in `RESULTS.md` is reproducible rather than remembered.

## Repository layout

```
migrations/   versioned SQL, applied in filename order, never edited once applied
scripts/      loaders, the migration runner, shared connection settings
sql/transform/  re-runnable layer builds, applied by scripts/transform.py
sql/          ad-hoc analysis queries and the R connection helper
tests/        pytest
figures/      generated diagnostics, not committed
exports/      generated Excel and Power BI extracts, not committed
docs/         execution plans, credibility derivation, business summary
```
