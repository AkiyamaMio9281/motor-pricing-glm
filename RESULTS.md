# RESULTS

Every number here names the script or query that produced it. Figures are
recorded as the layers land, not assembled at the end, so anything missing is
genuinely not done yet rather than pending a write-up.

Environment for all timings: Postgres 16 in Docker with stock settings
(`shared_buffers` 128 MB), Windows 11 host, Python 3.14.7.

---

## L1 · Data mart

### Source data

| Table | Rows | Columns | Source | MD5 |
|---|---|---|---|---|
| freMTPL2freq | 678,013 | 12 | OpenML 41214 | `f8875568bf0ca622929105197e2db613` |
| freMTPL2sev | 26,639 | 2 | OpenML 41215 | `24cc74449e3931cb1aad0d43a12e7a6e` |

Produced by `scripts/fetch_data.py`, which pins both MD5s and asserts both row
counts. OpenML's own description of 41214 states 677,991 rows, which disagrees
with the file it serves; see DEVLOG.

SHA-256 of the converted CSVs is written to `data/CHECKSUMS.txt` at fetch time.

### Raw load, method comparison

678,013 rows into `raw.freq_raw`, four runs each, same container:

| Method | Median | Throughput |
|---|---|---|
| `COPY FROM STDIN` | 392 ms | ~1.73 M rows/s |
| `executemany`, 10k batches | 12,080 ms | ~56 k rows/s |

Reproduce with `scripts/load_raw.py freq --method copy` and
`--method executemany`. Individual run times are in `raw.load_audit`, one row
per load, and in DEVLOG. Medians rather than means: one executemany run came in
at 16,455 ms with no identifiable cause, and it is kept in the record.

Severity table, 26,639 rows: 49 ms by COPY, 436 ms by executemany.

### Cleaning, reconciled

Every rule in `migrations/002_stg.sql` records its own row counts to
`stg.cleaning_audit`. Reproduce with
`SELECT * FROM stg.cleaning_audit ORDER BY table_name, rule_seq`.

| Table | Rule | Kind | Affected | Rows out |
|---|---|---|---|---|
| policy_cleaned | cast_text_to_types | transform | 1 | 678,013 |
| policy_cleaned | reject_duplicate_idpol | reject | 0 | 678,013 |
| policy_cleaned | reject_nonpositive_exposure | reject | 0 | 678,013 |
| policy_cleaned | reject_negative_claim_nb | reject | 0 | 678,013 |
| policy_cleaned | reject_unknown_category_level | reject | 0 | 678,013 |
| policy_cleaned | observe_exposure_above_one | observe | 1,224 | 678,013 |
| policy_cleaned | observe_claims_without_amount | observe | 9,116 | 678,013 |
| claim_cleaned | cast_text_to_types | transform | 0 | 26,639 |
| claim_cleaned | reject_nonpositive_claim_amount | reject | 0 | 26,639 |
| claim_cleaned | observe_orphan_claims | observe | 195 | 26,639 |

678,013 policy-years in and 678,013 out: this portfolio is clean on every
structural axis tested. The one transform is a single policy id written as
`1e+05` upstream.

### Observations carried forward

| Finding | Count | Decided in |
|---|---|---|
| Exposure above one policy-year | 1,224 rows, max 2.01 | 003 |
| Policies reporting claims the severity file does not price | 9,116 policies, 9,657 claims, 26.7% of reported claims | L3 |
| Claims whose policy-year is absent from the frequency file | 195 rows across 6 identifiers | 004 |

Other measured ranges: DrivAge 18-100, VehAge 0-100, BonusMalus 50-230,
VehPower 4-15, ClaimNb max 16, Exposure 0.0027 to 2.01. Claim amounts run from
1 to 4,075,400.56, totalling 60,697,931.

### Pricing decisions

`migrations/003_adjusted.sql` and `sql/transform/003_adjusted.sql`. Nothing is
removed; the uncapped values stay in `stg.policy_cleaned`.

| Decision | Kind | Policies | Basis |
|---|---|---|---|
| cap_exposure_at_one | transform | 1,224 | costs 139 exposure-years and no claims; dropping would cost 1,363 and 54 |
| flag_high_claim_count | transform | 9 | claim_nb > 4, max 16; counts are never capped |
| flag_short_exposure | transform | 13,603 | exposure < 0.02 yr, where proportionality measurably fails |
| observe_implied_rate_above_monthly | observe | 2,773 | implied rate > 12/yr, max 732 |

Modelling exposure after capping: 358,360 years against 358,499 raw, a loss of
139. Portfolio frequency is unchanged at 0.1007 to four places.

### Claims are not proportional to exposure

The measurement the short-exposure flag rests on. Reproduce by grouping
`stg.policy_cleaned` on exposure bands.

| Exposure band | Policies | Exposure-years | Claims | Claims per exposure-year |
|---|---|---|---|---|
| under a week | 13,603 | 101 | 362 | 3.58 |
| under 5 weeks | 107,950 | 6,825 | 2,793 | 0.41 |
| under half a year | 220,064 | 63,179 | 9,815 | 0.16 |
| half to one year | 335,172 | 287,031 | 23,078 | 0.08 |
| over a year | 1,224 | 1,363 | 54 | 0.04 |
| **whole portfolio** | **678,013** | **358,360** | **36,102** | **0.1007** |

Under the proportionality that a `log(Exposure)` offset asserts, the last column
would be flat at 0.1007. It spans a factor of 90, monotonically. The
short-exposure band holds 1.0% of claims on 0.03% of exposure.

Consequence for L2: the offset's central assumption is violated. This section
originally said "at the short end"; L2 found the violation runs through the whole
range of exposure and is not produced by the flagged rows. See Exposure in the
frequency model, below.

### Star schema

`migrations/004_dim_fact.sql` and `sql/transform/004_dim_fact.sql`.

| Table | Grain | Rows |
|---|---|---|
| dim.region | region | 22 |
| dim.area | density band | 6 |
| dim.vehicle | brand, fuel, power | 235 |
| dim.driver_band | reporting age band | 8 |
| dim.bonus_band | bonus-malus state | 4 |
| fact.exposure | policy-year | 678,013 |
| fact.claim | claim | 26,444 |

Every policy-year reaches the fact table; the count is asserted inside the
transform. Of 26,639 staged claims, 195 are excluded because their policy-year
is absent from the frequency file: six policies carrying 21 to 66 claims each,
788,714 in total amount.

Joining `fact.claim` to `fact.exposure` and summing exposure gives 18,286
exposure-years against a correct 17,270 for the same policies, because each
policy-year is counted once per claim. Aggregate claims first.

### Bonus-malus bands

Anchored on the French scale, not on quantiles.

| Band | Policies | Exposure-years | Claims per exposure-year |
|---|---|---|---|
| 50 maximum bonus | 384,156 | 225,233 | 0.0802 |
| 51-99 bonus | 266,533 | 123,454 | 0.1214 |
| 100 entry | 19,530 | 6,241 | 0.2758 |
| 101+ malus | 7,794 | 3,571 | 0.3758 |

Computed on raw exposure; the monotone rise holds on capped exposure too and is
asserted by a test.

### Area is a density band with inconsistent boundaries

All 22 regions span several areas, so area is not a regional attribute. It
bands density, but the densities 50, 100 and 500 are each assigned to two
areas, 4,012 policies at exactly the three internal boundaries. Area is taken
from the source and cannot be recomputed from density.

### Silent loss from an INNER JOIN, measured

A one-year gap in the driver bands, tested inside a rolled-back transaction:

| Join | Outcome |
|---|---|
| INNER JOIN | 667,712 rows; 10,301 policies dropped with no error |
| LEFT JOIN into NOT NULL key | transform fails on a row with driv_age 26 |

### Indexes and load performance

Measured by `scripts/explain_plans.py`, which regenerates
`docs/explain-plans.md` with full plans. Five runs per variant, medians; the
without-index variant drops the index inside a rolled-back transaction.

| Query | Index | Without | With | Change |
|---|---|---|---|---|
| Claims for one policy | `fact.claim_idpol_idx` | 0.53 ms | 0.01 ms | 44.3x faster |
| Delete the claim-free policy-years of one region | `fact.claim_idpol_idx` | 634.22 ms | 7.24 ms | 87.6x faster |
| Vehicle drilldown, region R43 (0.2% of policies) | `fact.exposure_region_key_idx` | 8.68 ms | 0.52 ms | 16.6x faster |
| Vehicle drilldown, region R24 (23.7% of policies) | `fact.exposure_region_key_idx` | 16.06 ms | 15.43 ms | no gain |
| Portfolio overview by driver band and bonus band | none | 33.55 ms | | |

Loading `fact.exposure`, 678,013 rows:

| Method | Median |
|---|---|
| INSERT with five per-row foreign-key triggers | 9,258 ms |
| suspend foreign keys, INSERT, restore set-based | 1,976 ms |

4.7x. EXPLAIN ANALYZE attributes about three quarters of the per-row load to
five foreign-key triggers firing 678,013 times each.

Deleting every claim-free policy-year, 643,953 rows, one run each and not part of
the regenerated document because the slow side takes five and a half minutes:

| | Total |
|---|---|
| without `claim_idpol_idx` | 328,607 ms |
| with `claim_idpol_idx` | 2,546 ms |

A covering index `(region_key) INCLUDE (vehicle_key, exposure, claim_nb)` was
measured and rejected: 13% faster on the broad region, 26 MB against 4.6 MB for
the plain index.

Timings below a second varied by a factor of two to three between the
exploratory and final measurement sessions. The document is the reference; the
magnitudes above held in both.

---

## L2 · Frequency model

### Model frame

`model.frequency_frame`, created by `migrations/006_model_frame.sql`. One row
per policy-year, rating factors as codes, exposure as float8, capped.

| | Value |
|---|---|
| Rows | 678,013 |
| Claims | 36,102 |
| Exposure-years | 358,360.105463 |
| Flagged short exposure | 13,603 |
| Levels: area, vehicle brand, fuel, region | 6, 11, 2, 22 |

R (`R/check_frame.R`) and Python (`scripts/frame.py`) read the view
independently and hash a canonical rendering of every row. Both produce
`2ecb69783bcd2387d8e00d6b88335ae2`. The comparison is shown to be sensitive: a
one-ulp change to a single exposure changes the hash, and rendering exposure
with 15 significant digits instead of 17 in R makes the test fail.

Load time from connect to validated frame, three runs each:

| Language | Load |
|---|---|
| R | about 1.4 s |
| Python | about 2.2 s |

### RPostgres bigint handling, measured

| Setting | Value beyond int32 | `idpol * 1.5`, ids 1, 3, 5 |
|---|---|---|
| default, integer64 | exact | 2, 5, 8 (silently rounded) |
| `bigint = "integer"` | NA, no warning | correct |
| `bigint = "numeric"` | exact | correct |

`R/db.R` uses `numeric`, and the frame loader converts `idpol` to integer after
a lossless check, because a double 100000 renders as `1e+05`.

### Exposure in the frequency model

From `docs/frequency-exposure.md`, generated by `R/frequency_exposure.R`. Same
frame and the same 38 rating parameters in every fit:
`veh_brand + veh_gas + region + driv_age + veh_age + bonus_malus + log(density) + veh_power`.
Area is omitted: its rank correlation with density is 0.976.

| Exposure enters as | Weighted annual frequency | Regular fuel vs diesel | Brand B12 vs B1 |
|---|---|---|---|
| offset(log(exposure)), the pricing model | 0.1007 | 1.053 | 1.167 |
| claim rate as response, exposure as weight | 0.1007 | 1.053 | 1.167 |
| claim count as response, exposure as weight | 0.0647 | 0.973 | 0.769 |
| offset(exposure), log omitted | 0.0784 | 1.078 | 1.045 |
| exposure left out | 0.0529 | 1.108 | 0.907 |

Observed: 36,102 claims over 358,360.1 policy-years, 0.1007. The rate-with-weights
model matches the offset model to within 5.3e-08 on every coefficient.

Proportionality, testing the offset's assumption by estimating the coefficient
on log(exposure):

| Fit | Coefficient | 95% interval | Std. errors below 1 |
|---|---|---|---|
| all policy-years | 0.3666 | [0.3546, 0.3786] | 103 |
| short exposure removed | 0.3991 | [0.3861, 0.4122] | 90 |

Fitted claims by exposure band under the offset model:

| Exposure | Observed | Offset fitted |
|---|---|---|
| under 1 week | 362 | 12 |
| 1 to 5 weeks | 2,793 | 780 |
| 5 weeks to 6 months | 9,815 | 7,381 |
| 6 months to 1 year | 23,132 | 27,929 |

Influence of the 13,603 short-exposure rows on the offset model: Pearson dispersion
2.648 with them and 1.951 without; the largest change in a key relativity is
0.45%, and in any coefficient 3.8%.

Pearson dispersion by specification, for D2-4: offset 2.648, log(exposure)
estimated 1.111.

### Banding the continuous terms

From `docs/frequency-banding.md`, generated by `R/frequency_banding.R`. Every
specification is an offset Poisson GLM. The rule, fixed before fitting: lowest
Poisson deviance on a holdout of every fifth risk group wins.

| Specification | Parameters | AIC | BIC | Holdout deviance |
|---|---|---|---|---|
| linear driver age, vehicle age, bonus-malus | 38 | 286,686 | 287,120 | 43,596.8 |
| equal-exposure decile bands | 63 | 284,503 | 285,223 | 43,165.8 |
| risk-structured bands | 66 | 281,959 | 282,713 | 42,707.9 |
| **risk-structured bands, vehicle power as a factor** | 76 | 281,679 | 282,547 | 42,648.8 |

Chosen, and BIC agrees:
`veh_brand + veh_gas + region + driv_age_band + veh_age_band + bonus_malus_band + log(density) + factor(veh_power)`.
Bands are the constants in `R/bands.R`.

Holdout: 116,307 risk groups, 135,455 rows, 7,221 claims. A risk group is a run of
consecutive policy ids with identical rating factors; 181,795 of 678,013 rows sit
in a group of two or more.

Out-of-sample actual over expected claims where the specifications differ most:

| Group | Holdout claims | Linear | Deciles | Chosen |
|---|---|---|---|---|
| driver age 19 | 47 | 1.626 | 1.477 | 0.930 |
| driver age 20 | 72 | 1.434 | 1.455 | 1.130 |
| vehicle age 0 | 1,014 | 2.172 | 1.842 | 0.982 |
| vehicle age 1 | 607 | 0.690 | 0.561 | 1.035 |
| bonus-malus 100 | 329 | 1.184 | 0.846 | 0.984 |
| vehicle power 8 | 379 | 0.810 | 0.791 | 0.976 |
| vehicle power 14 | 30 | 1.271 | 1.291 | 1.363 |

Density stays within 0.039 of 1 by decile under the linear term and is left as a
single log-linear slope.

Vehicle age 0 against 1, GLM relativity: 3.43 on all policy-years, 2.02 on exposures
of half a year or more. Mean exposure 0.289 at vehicle age 0, 0.551 for older
vehicles.

Coefficient on log(exposure) when estimated on the chosen terms: 0.411, standard
error 0.0063.

## L3 · Severity model

Not started.

## L4 · Pure premium and benchmark

Not started.

## L5 · Rate table and credibility

Not started.
