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
| L1 | Data mart: raw to staging to star schema to segment mart | SQL (Postgres) | through star schema, indexed |
| L2 | Claim frequency, Poisson GLM with exposure offset | R | offset model, banded terms, robust errors |
| L3 | Claim severity, Gamma GLM on claiming policies only | R | capped Gamma, terms chosen, diagnostics |
| L4 | Pure premium, gradient boosting baseline, validation | Python | pure premium, risk-group holdout, LightGBM baseline |
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

R on Windows is not on PATH by default. The R scripts run with the full path,
for example `"C:/Program Files/R/R-4.6.1/bin/Rscript.exe" R/check_frame.R`, and
Python finds R through `RSCRIPT` if set, then PATH, then the newest install
under Program Files.

The order matters and is enforced rather than documented. Migrations change
structure and are safe against an empty database; transforms read one layer and
rewrite the next, and `transform.py` exits non-zero rather than building a layer
out of an empty source. See `DEVLOG.md` for what happened before it did.

Every cleaning rule writes its own row counts to `stg.cleaning_audit`, so
the path from 678,013 raw rows to the staged table is reconciled rule by rule
rather than asserted. `SELECT * FROM stg.cleaning_audit ORDER BY table_name,
rule_seq` prints the chain; `RESULTS.md` carries the current values.

Cleaning and pricing decisions are separate layers. `stg.policy_cleaned` holds
what the file said, with malformed rows removed. `stg.policy_adjusted` holds
what the project decided to model on, beside it rather than over it, so a
decision can be revisited by re-running one transform.

The star schema is `fact.exposure` at policy-year grain and `fact.claim` at claim
grain, with five dimensions. Claims are not pre-aggregated: summing exposure
over a join of the two counts each policy-year once per claim, so aggregate
claims first. Every transform runs in one transaction, and a failed rebuild
leaves the previous good tables in place.

Both modelling languages read one view, `model.frequency_frame`, and do no
joins of their own. A test renders every row in R and in Python and requires
identical md5s, so the frequency GLM and the Python benchmark are fitted on
provably the same data:

```bash
.venv/Scripts/python scripts/frame.py    # Python fingerprint
Rscript R/check_frame.R                  # R fingerprint, must match
```

Every frequency fit goes through `fit_frequency()` in `R/frequency.R`, which
always uses `offset(log(exposure))` and refuses to return a fit whose claims do
not balance to the observed total. `R/frequency_exposure.R` fits every other way
exposure could be written into the model and generates
`docs/frequency-exposure.md`. The short version: a weight on the claim rate is
the same model as the offset, a weight on the claim count flips the fuel
relativity, and the data rejects the proportionality the offset assumes, which is
kept for pricing anyway and the document says why.

Driver age, vehicle age and bonus-malus enter as fixed bands from `R/bands.R`,
and vehicle power as a factor. `R/frequency_banding.R` compared that with linear
terms and with equal-exposure decile bands on a holdout split by risk group, under
a rule fixed before fitting, and writes `docs/frequency-banding.md`. The deciles
merge the youngest drivers with 29-year-olds and new cars with one-year-old ones,
which is where risk changes fastest, and they recovered less than half the holdout
improvement of bands placed on the risk structure.

```bash
Rscript R/frequency_exposure.R   # about 90 s; tests fail if the document goes stale
Rscript R/frequency_banding.R    # several minutes; same staleness checks
Rscript R/frequency_dispersion.R # a few minutes
Rscript R/severity_population.R  # a couple of minutes
Rscript R/severity_large_losses.R
Rscript R/model_diagnostics.R    # writes the figures in docs/figures/
Rscript R/export_predictions.R   # about 3 min; six GLM runs into model.glm_prediction
.venv/Scripts/python scripts/pure_premium_report.py
.venv/Scripts/python scripts/holdout_report.py
.venv/Scripts/python scripts/lightgbm_baseline.py  # about 8 min; tunes, fits and stores the benchmark
```

Pure premium is assembled in Python from the GLM predictions R writes to Postgres, with
the unit in every column name: `claims_per_year` times `capped_amount_per_claim` times
the large-loss load is `amount_per_year`, and a row's expected loss is its exposure
times that. `pure_premium()` checks each quantity against the data before multiplying
and rebalances to recorded losses only within 1%, because every units error tried in
`docs/pure-premium.md` gets its total right after a rebalance and its prices wrong.

Frequency for pricing counts the 26,444 claims that carry an amount, not all 36,102
reported. That choice is not a level adjustment: 77.3% of the claims reported on new
vehicles have no amount, and the new-car relativity is 3.43 on reported claims and
0.98 on priced ones. The data cannot say what an unpriced claim costs, and the
document sets out both readings.

Validation holds out every fifth risk group, not a hashed fifth of policy ids. IDpol is unique,
so a split on it is a row split, and it scatters pieces of one policy-year across both
sides. `docs/holdout-split.md` measures the cost on rows both splits hold out: the
pieces share their reported claim counts, 1,828 groups where every piece reports a claim
against 105 expected, and a model that remembers exact profiles gains up to half of
what all the rating factors achieve from that leak on `ClaimNb`. Priced claims are
rarely shared, 51 groups against 35, and on them there is no leak. Both splits live in
one view, `model.holdout`, and the GLM runs used for validation are fitted on its
training rows.

The benchmark is LightGBM with a Tweedie objective, trained on the GLM's training rows with
the same cap, load and exposure basis, and tuned by cross-validation folded by risk group.
`docs/lightgbm-baseline.md` records that Tweedie boosting satisfies its objective while its
total drifts to 96% of capped losses, so the benchmark carries an explicit balance
correction rather than a wider tolerance. The same run confirms D3-2's leak with LightGBM
itself: on reported claims, row folds score 0.31% better and boost 57% longer. On the
holdout LightGBM's capped Tweedie deviance is 6.06% below a constant against the GLM's
5.22%, a gap D3-6 tests.

The pricing severity model is `fit_pricing_severity()`: claims capped at 34,377, the
99.5th percentile, with the 25.3% of losses above it restored by a flat load of
1.3391. Capping rather than dropping matters; dropping the same claims would leave
pure premium a third short. `docs/model-diagnostics.md` shows the Gamma distribution
fitting badly, capped or not, because 40.7% of claims sit on four exact amounts, so
the severity model is used for its mean and never for its distribution.

Severity is fitted through `fit_severity()` on `model.severity_frame`, one row per
priced claim, and refuses missing or non-positive amounts by name rather than letting
`glm()` drop them silently. `docs/severity-population.md` measures the larger issue:
the frequency model counts 36,102 reported claims and only 26,444 have an amount, so
reported-claim frequency times severity is 36.5% above recorded losses, and the
unpriced claims are concentrated on newer vehicles and particular regions. Which
count pure premium uses is decided when pure premium is assembled.

Standard errors for the frequency model come from `frequency_vcov()`, a sandwich
estimator clustered by risk group. The Pearson dispersion is 2.4, but nearly half
of it comes from 0.1% of rows where the exposure relationship misfits; quasi-Poisson
would widen every standard error by 54% where the clustered sandwich finds 8%.
`docs/frequency-dispersion.md` judges each dispersion statistic against its own
simulated distribution rather than against 1, which is how the two textbook
estimators come to point in opposite directions.

Indexes were added only where `EXPLAIN ANALYZE` showed they pay, and one that
did not was rejected. `docs/explain-plans.md` holds the plans and is generated,
not written:

```bash
.venv/Scripts/python scripts/explain_plans.py
```

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
sql/explain/    queries measured by scripts/explain_plans.py
sql/          ad-hoc analysis queries
R/            R side: connection, model frame, GLMs
tests/        pytest
exports/      generated Excel and Power BI extracts, not committed
docs/         generated analyses, their figures in docs/figures/, credibility derivation
```
