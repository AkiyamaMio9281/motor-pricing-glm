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

Consequence for L2: the offset's central assumption is violated at the short
end, so the frequency model is refitted with and without the flagged rows and
the coefficient movement reported.

---

## L2 · Frequency model

Not started.

## L3 · Severity model

Not started.

## L4 · Pure premium and benchmark

Not started.

## L5 · Rate table and credibility

Not started.
