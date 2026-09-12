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

---

## L2 · Frequency model

Not started.

## L3 · Severity model

Not started.

## L4 · Pure premium and benchmark

Not started.

## L5 · Rate table and credibility

Not started.
