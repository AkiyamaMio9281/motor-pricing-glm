# DEVLOG

What went wrong and what it cost. Entries are written in the same commit as the
change they describe, so the dates here match the history.

---

## 2026-09-12 · Two Python 3.14 installations, one of them empty

Installed the project's Python dependencies, then could not import lightgbm.
The packages were installed; they were installed into a different interpreter.

There are two Python 3.14 runtimes on this machine with separate
`site-packages`:

| Invocation | Prefix | Version |
|---|---|---|
| `python` (first on PATH) | `C:\Python314` | 3.14.7 |
| `py -3.14` | `...\AppData\Local\Python\pythoncore-3.14-64` | 3.14.5 |

`py --list` marks the second one as the default with an asterisk, but PATH
resolves `python` to the first. So `pip install` under one invocation and
`python script.py` under the other disagree about what is installed, with no
error that points at the cause. `pip list` looked correct the whole time,
because it was reporting on whichever interpreter had just been asked.

A third runtime, an Astral-managed CPython 3.12.14, was kept in reserve in case
Python 3.14 wheels were missing. They were not: lightgbm 4.7.0,
scikit-learn 1.9.1, psycopg 3.3.5 and openpyxl 3.1.5 all resolved as binary
wheels under `--only-binary=:all:` on 3.14, so the fallback was dropped.

**Fix.** A project venv, and no bare `python` anywhere. Every command in the
README and every subprocess call names `.venv/Scripts/python`. Remembering
which of three interpreters is correct is not a fix; removing the choice is.

---

## 2026-09-12 · Why the raw layer is all text

First instinct was to type the raw tables properly, with `exposure numeric` and
`idpol bigint`, and let `COPY` reject bad rows at the door.

That is the wrong place for the check. It collapses two different failures into
one error message: a malformed download and a genuinely dirty value both come
back as "invalid input syntax for type numeric". The first is a transport
problem and the second is a data problem, and they have different fixes.

So `raw.freq_raw` and `raw.sev_raw` are entirely `text`. A load either succeeds
or has failed to move bytes. Casting happens in `002_stg.sql`, where a rejected
value is attributable to a named cleaning rule with a row count next to it.

The cost is one extra pass over 678k rows. The benefit is that when something
breaks at 2am the error already says which of the two things it was.

---

## 2026-09-12 · Migrations are frozen once applied

`scripts/migrate.py` records the sha256 of every migration it applies and
refuses to run if an already-applied file has changed on disk. This started as
a nicety and turned out to matter within the hour: while checking that the raw
DDL was right I edited `001_raw.sql` after it had already run, which would have
left this database and a fresh clone with silently different schemas.

Refusing is better than warning here. The remedy is a new migration, or
`--reset` to rebuild, and both leave a reproducible mart. Verified: exit code 1
on drift, and `--status` reports `APPLIED, FILE CHANGED`.

---

## 2026-09-12 · OpenML's row count disagrees with the file OpenML serves

The description text for OpenML dataset 41214 says freMTPL2freq "contains risk
features for 677,991 motor third-part liability policies". The CASdatasets
original, and every paper that uses it, says 678,013.

Counted the file that OpenML actually serves: 678,013 data rows. So the
description is wrong and the data is right, which is the better of the two ways
that could have gone.

Worth the ten minutes it cost, because the alternative was writing 677,991 into
the row-count assertion in `fetch_data.py` and then spending much longer on why
a correct download kept failing validation. The lesson is narrow and practical:
a dataset's prose description is not a checksum. `fetch_data.py` now asserts the
count against the file, and the constant carries a comment saying why it
disagrees with the upstream page.

Both files' MD5s do match what OpenML publishes, so this is a documentation
error on their side, not a mirror serving different data.

---

## 2026-09-12 · COPY is 31x faster than batched INSERTs, and the header is why

Loading 678,013 rows into `raw.freq_raw`, four runs of each method against the
same container:

| Method | Runs (ms) | Median |
|---|---|---|
| `COPY FROM STDIN` | 361, 385, 398, 471 | **392 ms** |
| `executemany`, 10k batches | 11,590, 12,001, 12,159, 16,455 | **12,080 ms** |

About 31x. The 16,455 ms run is an outlier with nothing to blame it on; it is
left in the table rather than dropped, and the median is used instead of the
mean so it does not do the talking.

The gap is larger than "fewer round trips" explains on its own. `executemany`
also parses all 36 MB in Python and builds 678,013 tuples before any of it
reaches the socket, while the COPY path never decodes the file at all -- it
reads 1 MB blocks and writes them straight to the connection. That asymmetry is
not a flaw in the comparison to be corrected for. It is most of the mechanism,
and it is why the measured question is "what does it cost to get this file into
that table", end to end, rather than a narrower one about protocol overhead.

Two decisions came out of this.

**`pandas.to_sql` was not measured.** The plan named it as the slow side of the
comparison, but `to_sql` under pandas 3 needs SQLAlchemy, and installing a
dependency in order to demonstrate an approach the project then discards is a
bad trade. `to_sql` batches INSERTs through SQLAlchemy, so `executemany` on the
same driver measures the same mechanism with one less layer in the way, and the
comparison stays about loading rather than about ORMs.

**Both methods stayed in the code.** The slow one is not dead weight: it is what
makes the number in this entry reproducible by anyone who clones the repository,
and `--limit` makes it cheap to re-run. A benchmark whose losing branch has been
deleted is an anecdote.

---

## 2026-09-12 · One policy id is written as `1e+05`

Row 49346 of the upstream ARFF has `1e+05` where every other row has an
integer. It is scientific notation for 100000, almost certainly from a
float round-trip somewhere in the export that produced the file.

Three things had to be checked before it could be called harmless.

Is `100000` also present as an ordinary row? No. So resolving the notation
creates no duplicate identifier. Had it been present, this would have been a
primary key collision discovered at the `ALTER TABLE ... ADD PRIMARY KEY` at the
bottom of 002, with no obvious cause.

Does the same problem exist in the severity file? No, all 26,639 identifiers
there are plain integers.

Does the cast actually work? `'1e+05'::bigint` raises `invalid input syntax for
type bigint`. `('1e+05'::numeric)::bigint` gives 100000. So the cast in 002
routes through numeric, and the migration says why at the point where it does.

This is the first real return on the decision recorded earlier to keep the raw
layer entirely text. With a typed raw layer, this one row would have aborted the
COPY of all 678,013, and the error would have pointed at the loader.

## 2026-09-12 · A quarter of the reported claims have no amount

Counting before modelling turned up the finding that will shape L3.

| | Policies | Claims |
|---|---|---|
| Reporting at least one claim | 34,060 | 36,102 |
| With no matching row in the severity file | 9,116 | 9,657 |

So 26.7% of reported claims carry no amount. The severity file also never has
more claims for a policy than the frequency file reports, and never has a claim
for a policy reporting none, so this is one-directional: the frequency file
knows about claims the severity file does not price.

The consequence is sharper than the usual warning about severity models. The
standard advice, and what the project plan says, is to fit severity only where
`ClaimNb > 0`. That is necessary and it is not sufficient. 9,116 of those
policies have nothing to fit on. Whatever the severity model is fitted on, it is
a different effective population from the frequency model, and multiplying the
two predictions to get pure premium quietly assumes they are not.

Recorded as an observation in `stg.cleaning_audit` rather than acted on. The
decision belongs with the severity model, and a count in the audit trail is what
that decision will have to argue against.

Separately: the 195 orphan claims, whose policy-year is missing from the
frequency file entirely, come from just six identifiers carrying 21 to 66 claims
each. Ordinary policies do not look like that. They are left in place until 004,
where the foreign key removes them for a referential reason rather than a
judgement about their contents.

## 2026-09-12 · Making the audit table falsifiable

The cleaning audit is only worth having if it can be wrong and be caught. A
`rows_out` column written by hand looks exactly as convincing as one that is
true.

So `stg.record_rule` never takes `rows_out` as an argument. It counts the table
itself, at the moment the rule finishes. The caller supplies only the name and
how many rows its own statement touched.

And the test re-derives the chain rather than reading it: for each table, in
rule order, `rows_out` must equal the previous `rows_out` minus `rows_affected`
for a reject, and must be unchanged for an observation, with the last value
matching a fresh `count(*)`. Verified that it fails when it should by editing
one `rows_out` by seven and watching the assertion name the rule.

There is a related trap the chain cannot catch on its own. A reject rule whose
predicate is simply wrong reports zero removals, which is indistinguishable from
clean data. So the predicates are re-asserted against the finished table in a
separate test. Five of the six reject rules here legitimately remove nothing;
without that second test there would be no evidence they were ever capable of
removing anything.

## 2026-09-12 · A staging layer built from an empty raw layer, and a green test run

The worst bug so far, because nothing failed.

`002_stg.sql` both created the staging tables and filled them from
`raw.freq_raw`. But `migrate.py --reset` drops the project schemas and applies
every migration in order, and that happens *before* any data is loaded. So the
documented rebuild sequence produced this:

```
migrate.py --reset     # 001 creates raw (empty), 002 reads raw (empty)
load_raw.py all        # raw now has 678,013 rows
migrate.py             # "nothing to apply" -- 002 is already recorded
```

Final state: a fully loaded raw layer and a staging layer of zero rows, with no
command in the project that would fix it.

Two things made this survivable long enough to be interesting.

**The audit trail reconciled.** All seven rules recorded, every one reporting
`rows_affected = 0` and `rows_out = 0`. The chain closes perfectly, because
zero minus zero is zero. An accounting of nothing is still a valid accounting,
and the very test written to prove the audit table is not decorative passed.

**The test suite was green.** `14 passed, 8 skipped`. The `staged` fixture
skipped when `stg.policy_cleaned` was empty, on the reasoning that a fresh clone
with no data should not produce a wall of failures. That reasoning is right for
a fresh clone and wrong here, and the fixture could not tell the two apart.

The fix separates two things that had been conflated.

| | Migration | Transform |
|---|---|---|
| Changes | structure | data |
| Runs | once, then frozen | whenever the layer below changes |
| Safe on an empty database | yes | refuses |

`migrations/002_stg.sql` now creates tables and the audit function only.
`sql/transform/002_stg.sql` holds the rules, truncates before it writes, and is
re-run freely. `scripts/transform.py` checks that every required source is
non-empty before it runs anything, and exits 1 with the correct command to run
instead. The fixture now distinguishes the two cases: no data at all skips, data
in raw with nothing in staging fails and says which script was not run.

The general lesson is about the shape of the check, not this pipeline. A test
whose precondition can be satisfied by the failure it is meant to detect reports
success. "Skip if empty" is that shape whenever empty is a possible symptom.

The staging tables also lost their CHECK constraints in this refactor, which
looks like a regression and is not. The rules insert everything and delete per
rule, and that is what makes the row-count chain reconcile; a CHECK would reject
rows at insert time and leave the rules with nothing to count. Constraints
belong on the star schema in 004, after the rules have run.

---

## 2026-09-12 · Capping exposure is easy; the interesting number was next to it

The exposure decision took ten minutes and is not worth much discussion. 1,224
policy-years run past a year, to 2.01. Capping costs 139 exposure-years out of
358,499 and no claims. Dropping costs 1,363 exposure-years and 54 claims, and
biases portfolio frequency upward, because the over-one band has the lowest
frequency in the book. A policy-year longer than a year is a bookkeeping
artefact, not a policy exposed for longer than it existed. Cap it.

What the same query turned up beside it is the finding.

The plan for this commit was to flag policies with an extreme claim count. Nine
policies report more than four claims. Listing them with their exposure made it
obvious the claim count was the wrong thing to be looking at:

| Policy | Claims | Exposure | Implied annual rate |
|---|---|---|---|
| 3254353 | 11 | 0.07 | 157 |
| 3253234 | 11 | 0.08 | 138 |
| 2248174 | 9 | 0.08 | 113 |
| 93954 | 5 | 1.00 | 5 |

Five claims in a full year is a bad driver. Eleven claims in three and a half
weeks is not a driver at all. So the question moved from the count to the count
divided by exposure, and from nine policies to the whole book.

**Claims are not proportional to exposure in this portfolio, and the departure
is enormous.**

| Exposure | Policies | Exposure-years | Claims | Claims per year |
|---|---|---|---|---|
| under a week | 13,603 | 101 | 362 | 3.58 |
| under 5 weeks | 107,950 | 6,825 | 2,793 | 0.41 |
| under half a year | 220,064 | 63,179 | 9,815 | 0.16 |
| half to one year | 335,172 | 287,031 | 23,078 | 0.08 |
| over a year | 1,224 | 1,363 | 54 | 0.04 |

Under proportionality that last column is flat at the portfolio rate of 0.10.
It spans a factor of ninety instead, monotonically decreasing in exposure. The
probability of having any claim says the same thing from the other direction: a
policy exposed under a week has a 2.5% chance of a claim where proportionality
predicts about 0.06%, against 6.5% for a full year.

This matters because `log(Exposure)` as a Poisson offset *is* the assertion that
expected claims are proportional to exposure. That assertion is the one thing
D2-2 exists to get right, and the data says it is false in a measurable,
systematic way at the short end.

The likely mechanism is not error but selection. A policy that has a claim is
more likely to end soon after -- cancellation, a write-off, a change of insurer
-- so exposure is partly an outcome of the claim rather than something that
preceded it. Nothing in a cleaning layer can fix that, and deleting the rows
would be worse: 13,603 policies carrying 362 claims, 1% of all claims, on 0.03%
of exposure. That is the precise shape of a high-leverage point in a Poisson
fit, and quietly removing it would improve every diagnostic while changing the
answer.

So the decision here is to flag and keep. `stg.policy_adjusted` carries
`is_short_exposure` at a one-week threshold, where the departure stops being
gradual, and `implied_rate` computed once so that no downstream diagnostic picks
its own definition. D2 refits with and without those rows and reports whether
the coefficients move. That is a result, not a cleaning rule, and it belongs in
the model layer.

Two smaller decisions came out of the same look.

**Claim counts are flagged, never capped.** Some published treatments of this
dataset cap `ClaimNb` at 4. That discards observed claims to tidy a histogram,
on 0.001% of policies, in a model that already has an offset for how long each
policy was exposed.

**Adjustments live beside the data, not over it.** `stg.policy_adjusted` is a
separate narrow table rather than an edit to `stg.policy_cleaned`. The cleaned
layer keeps what the file said. Someone who disagrees with the one-week
threshold changes one transform and re-runs it, instead of rebuilding the mart
from the CSV to recover the original exposure.

The test for the threshold re-derives the band measurement instead of asserting
the constant. A threshold justified by a number in a comment is a threshold that
can stop matching its data without anything noticing.

---

## 2026-09-12 · The foreign key failed, and its error was the least useful part

Built the star schema with every constraint declared up front, and loaded
`fact.claim` from every staged claim. The foreign key refused it:

```
psycopg.errors.ForeignKeyViolation: insert or update on table "claim"
    violates foreign key constraint "claim_idpol_fkey"
DETAIL:  Key (idpol)=(2262511) is not present in table "exposure".
```

This is the textbook failure, and it was expected. What was worth writing down
is how little the message says. It names one key and stops. The real violation
is 195 claims across six policies, each carrying 21 to 66 claims, and policy
2262511 is only reported because it happens to be the largest.

Nothing about the scope came from the error. It came from
`observe_orphan_claims = 195`, recorded in 002 three commits ago, which is the
argument for recording observations at all: the constraint did not discover the
problem, it enforced a decision that had been deliberately deferred to the layer
where it belongs. Without the observation, the next step after this error would
have been a query to find out how bad it was.

The decision is to exclude them. A claim with no policy-year has no exposure to
price against, so it cannot enter the frequency model; in the severity model it
would contribute amounts from policies whose rating factors are unknown, and
there is nothing to impute them from. 788,714 in claim amount leaves with them.

Two things about the failure itself were checked rather than assumed.

**The failed transform left nothing half-built.** Each transform runs in one
transaction, so after the error all seven tables in the layer were empty rather
than holding dimensions with no facts. Then, after the fix, reintroducing the
bad insert and running again left the *previous good* star schema fully intact
at 678,013 and 26,444 rows. `TRUNCATE` is transactional in Postgres, so the
rollback restores the old contents too. A failed rebuild does not destroy the
last good mart, which is the property that matters on the day a rebuild fails.

**`transform.py` printed a raw Python traceback.** Replaced it with the
transform name, Postgres's primary message and detail, and an explicit line
saying the transaction was rolled back and later transforms did not run. The
first question after a failed rebuild is whether the mart is now broken, and the
output should answer it.

## 2026-09-12 · An INNER JOIN to a band table can lose 10,301 policies silently

The fact table is loaded by joining staged policies to their dimensions. For
region, area and vehicle, those dimensions are built from the same staged rows,
so every value has a match. The two band dimensions are different: their ranges
are written by hand, and a range can have a gap.

Measured what a gap costs, inside a transaction that was rolled back. Moving the
lower bound of the 26-30 driver band to 27, so that exactly one age has no band:

| Join | Result |
|---|---|
| `INNER JOIN` | 667,712 rows, **10,301 policies silently dropped**, no error |
| `LEFT JOIN` into a `NOT NULL` key | fails, naming a row with `driv_age = 26` |

A one-year gap removes 1.5% of the book, and every downstream number would be
computed confidently on the remaining 98.5%. So the transform uses `LEFT JOIN`
into `NOT NULL` keys throughout, including the dimensions that cannot currently
have gaps, because the point is that the failure mode is loud by construction
rather than absent by luck. An overlap fails the other way: the policy matches
two bands and the primary key refuses the duplicate `idpol`. A count assertion
at the end closes the last case, a fan-out that duplicates nothing.

The same experiment showed the dimension protecting itself: deleting the band
failed first, because `fact.exposure` still referenced it.

## 2026-09-12 · Area is not a property of region

The plan listed four dimensions, with area folded into region. Checked the
functional dependency before building it, and it does not hold: all 22 regions
span several areas. Area is a banding of *density* instead, A through F running
from under 50 inhabitants per km2 to over 10,000.

It is not a clean banding either. The densities 50, 100 and 500 each appear
under two different areas -- exactly the three internal boundaries, 4,012
policies in all. The upstream derivation assigned boundary values
inconsistently, so area cannot be recomputed from density and has to be taken
from the source. It gets its own dimension, carrying its density range as a
description rather than a rule.

The modelling consequence is for D2: area and density carry close to the same
information, so putting both in the frequency GLM is near-collinear by
construction.

## 2026-09-12 · Two banding choices, and why they are not the modelling bands

Driver age uses conventional motor bands, narrow at the young end where risk
moves fastest. Bonus-malus is banded on the meaning of the French scale rather
than on its distribution: 100 is the entry coefficient, each claim-free year
takes 5% off to a floor of 50, each claim adds 25%. So exactly 50, below 100,
exactly 100 and above 100 are four different histories, and the observed
frequency rises monotonically across them, 0.08 to 0.12 to 0.28 to 0.38. A test
holds that monotonicity.

These are *reporting* bands, fixed so that the mart and the Power BI extract have
categories that do not move. The fact table keeps the raw continuous values
beside the band keys, because D2-3 chooses modelling bands from the data, and a
rating plan is allowed to report on one banding and price on another.

## Open for D1-6: transform 004 takes ten seconds

Transforms 002 and 003 run in one to two seconds. 004 takes nine to ten. The
likely cost is per-row constraint checking: five foreign keys and two CHECK
constraints evaluated on each of 678,013 inserted rows. Not investigated here,
because measuring it properly is what D1-6 is for, and a guess recorded as a
finding would be worse than an open question recorded as one.

---

## 2026-09-12 · The load was slow because of foreign keys, and the fix had to avoid copying them

D1-5 left an open question: transform 004 took nine to ten seconds against one
to two for the others, and the guess was per-row constraint checking. It was
answered with `EXPLAIN ANALYZE` on the INSERT, which reports each foreign-key
trigger separately:

```
Trigger for constraint exposure_region_key_fkey:      time=1735 ms  calls=678013
Trigger for constraint exposure_area_key_fkey:        time=1760 ms  calls=678013
Trigger for constraint exposure_vehicle_key_fkey:     time=3495 ms  calls=678013
Trigger for constraint exposure_driver_band_key_fkey: time=1839 ms  calls=678013
Trigger for constraint exposure_bonus_band_key_fkey:  time=1756 ms  calls=678013
Execution Time: 13951 ms
```

Three quarters of the time is five triggers each doing 678,013 lookups. The
guess was right, which is worth less than it sounds: I had suspected the band
joins just as confidently, and the plan shows those cost nothing. Postgres puts
a Memoize node over each band lookup, and 678,013 lookups of driver age resolve
with 83 cache misses, one per distinct age.

The standard fix is to drop the constraints, load, and add them back, so each is
validated by one set-based query. Measured over two sessions, that took the load
from about ten seconds to about two, 4.7x to 5.7x, with the set-based validation
of all five under 200 ms.

The standard *implementation* is the thing to avoid. Writing the ADD CONSTRAINT
statements into the transform creates a second copy of every foreign key
definition, and two copies drift: change a constraint in a migration and the
next rebuild silently re-creates the old one. So `meta.suspend_foreign_keys`
reads the definitions out of `pg_constraint` before dropping them, and
`meta.restore_foreign_keys` puts back exactly what it read. The migration stays
the only place a constraint is defined.

That introduces one new way to fail, a constraint dropped and never restored,
and it is closed three times over: the sequence runs in the transform's single
transaction, `meta.assert_foreign_keys_restored()` raises before commit if
anything is still stashed, and a test checks every constraint exists afterwards.

One subtler hole was closed as well. `pg_get_constraintdef` omits the schema of a
referenced table when that schema is on the search path, so under a search path
containing `dim` the stash would read `REFERENCES region(region_key)` and could
restore against a different `region`. The functions pin their search path to
`pg_catalog`, and a test suspends under a search path that includes `dim` to
prove the stashed text is still qualified.

Transform 004 now runs in about three seconds, including the index maintenance
described below.

## 2026-09-12 · The index that matters is on the side Postgres does not index

Postgres indexes the *referenced* side of a foreign key automatically, because it
has to be a primary key or unique. It never indexes the *referencing* side.
`fact.claim.idpol` references `fact.exposure` and had no index.

No query that reads claims in bulk notices. Anything that removes a policy-year
does, because deleting a row from `fact.exposure` must confirm no claim still
references it, and without an index that check is a sequential scan of
`fact.claim` for every row deleted. Measured by deleting every claim-free
policy-year, one run each:

| | Total | Foreign-key trigger |
|---|---|---|
| without `claim_idpol_idx` | 328,607 ms | 328,282 ms over 643,953 calls |
| with `claim_idpol_idx` | 2,546 ms | 2,299 ms |

Five and a half minutes against two and a half seconds, 129x, and essentially all
of it is the trigger at 0.51 ms a row. The mart is rebuilt by TRUNCATE, which
skips the check, so today's pipeline never pays this. It is the cost waiting for
the first person who corrects one region with a DELETE. A test now fails if any
foreign key into a fact table lacks an index on its referencing column.

## 2026-09-12 · The same index is 16x faster on one region and useless on another

`exposure_region_key_idx` was added for drilldowns into a single region, and
measured on the smallest and the largest:

| Region | Share of policies | Without | With |
|---|---|---|---|
| R43 | 0.2% | about 9 ms | under 1 ms |
| R24 | 23.7% | about 16 ms | about 16 ms, no gain |

Reading a quarter of the table costs the same through an index as without one.
The index is kept because most regions are small, and it is documented as
helping only for those, rather than as "the drilldown index".

A covering index with `INCLUDE (vehicle_key, exposure, claim_nb)` was measured as
the obvious next step for the broad case. It bought 13% on R24. It is also 26 MB
against the plain index's 4.6 MB, 5.8x, because a B-tree deduplicates repeated
keys, which makes an index over 22 distinct regions very small, and an index with
INCLUDE columns cannot be deduplicated. Half the size of the table for 13% on one
query shape. Rejected, and the rejection is written into the migration so that
nobody adds it back as an obvious improvement.

## 2026-09-12 · Four of five dimension tables had never been analysed

Found while trying to explain why the vehicle foreign-key trigger costs twice
what the other four do. The plan each trigger runs showed the vehicle lookup as a
sequential scan and the region lookup as an index scan, which looked like the
explanation. It was not, but the reason the plans differed was a real finding:

| Dimension | relpages | reltuples |
|---|---|---|
| region, area, driver_band, bonus_band | 0 | **-1** |
| vehicle | 2 | 235 |

`reltuples = -1` means never analysed. Autovacuum analyses a table once its
modifications pass 50 rows plus 10% of its size, and every rebuild truncates the
dimensions back to empty. A table of 4 to 22 rows cannot accumulate 50
modifications between truncates, so autovacuum will never analyse it, and the
planner had been guessing the size of every small dimension since D1-5. Only
`dim.vehicle`, at 235 rows, ever crossed the threshold. Transform 004 now
analyses all five dimensions explicitly, and a test checks each has statistics.

With the region dimension analysed, its lookup switched to a sequential scan as
well, and the vehicle trigger was still twice the others. So the access path was
never the explanation. The one stable difference is that `dim.vehicle` is the
only dimension larger than one page. The load no longer runs per-row triggers,
so this was not pursued, and it is recorded as observed rather than explained.

## 2026-09-12 · A statistics test that raced autovacuum

The first test for fresh statistics asserted `n_mod_since_analyze = 0` on the
fact tables, since transform 004 ends with ANALYZE. It failed straight after a
rebuild. The easy reading was that ANALYZE inside a transaction does not see the
rows that transaction inserted, which would have meant the statistics were
wrong and the fix belonged somewhere else entirely.

Checked it directly, inside one transaction: TRUNCATE sets `reltuples` to -1,
inserting 12,345 rows leaves it at -1, and ANALYZE sets it to exactly 12,345. A
count that distinctive rules out a stale leftover. In-transaction ANALYZE does
see the new rows, and the statistics had been correct.

What is wrong is the counter. The INSERT's modifications are reported to the
statistics system at commit, after the ANALYZE has already reset the counter, so
it climbs straight back to 678,013. Autovacuum then sees what looks like an
unanalysed table and re-analyses it about a minute later, at which point the
counter reads zero. The timestamps show both: the transform's ANALYZE at
02:08:00, autovacuum's at 02:08:40.

So the test was a race, red for the first minute after a rebuild and green after,
while the statistics were right throughout. It now checks what the planner
actually reads, `reltuples` within 1% of the true count and `pg_stats`
populated, and it passes when run in the same second as the rebuild with the
counter still at 678,013.

## 2026-09-12 · The plan document regenerates itself

`docs/explain-plans.md` is not a transcript of queries run once by hand. It is
generated by `scripts/explain_plans.py` from the queries in `sql/explain/`,
against the committed schema.

Measuring a query without its index relies on DROP INDEX being transactional in
Postgres: drop it inside a transaction, run EXPLAIN ANALYZE, roll back. The index
is back before the next run, so both variants are measured from the same final
schema and nothing has to be rebuilt to show what it was like before. Every run,
including the DELETEs, is in its own rolled-back transaction and starts from the
same data. The load comparison reads its INSERT out of transform 004 rather than
copying it, for the same reason the foreign keys are read from the catalogue.

Generating it is also what showed that sub-second timings are not stable across
sessions. The narrow-region drilldown measured 11x in the exploratory session and
16x in the final one; the single-policy claim lookup went from 16x to 44x. So the
migration comments carry only magnitudes that held in both sessions, and the
exact numbers live in the document that can be regenerated.

---

## 2026-09-12 · One model frame, read by two languages, checked to the byte

The frequency GLM is fitted in R and benchmarked in Python. If each language
built its own frame from the fact tables, there would be two definitions of a
policy-year and two sets of numbers that disagree for reasons unrelated to
modelling. So there is one view, `model.frequency_frame`, and neither loader
joins anything.

That is easy to claim and easy to check, so it is checked. Both languages render
every row as canonical text and hash it. On the first run:

```
R       {"rows": 678013, "claims": 36102, ..., "md5": "2ecb69783bcd2387d8e00d6b88335ae2"}
Python  {"rows": 678013, "claims": 36102, ..., "md5": "2ecb69783bcd2387d8e00d6b88335ae2"}
```

Two further tests make sure the equality means something. Moving one exposure
value to the next representable double changes the hash, so the rendering is
fine enough to see a one-bit difference. And rendering exposure in R with
`%.15g` instead of `%.17g` makes the cross-language test fail, so the
comparison really does compare. That second experiment had a side result: the
two renderings differ in text on 403,288 of 678,013 rows, but `%.15g` still
round-trips every value in this data to the same double. Seventeen digits are
used anyway, because seventeen is what guarantees a round trip for *any* double,
and fifteen happening to suffice for this portfolio is a property of the data,
not something to build on.

## 2026-09-12 · Every bigint setting in RPostgres fails silently except one

RPostgres has three ways to bring a Postgres `bigint` into R. Measured all three
against this database before choosing, which turned out to matter, because the
first probe pointed at the wrong answer.

| Setting | A value beyond int32 | `idpol * 1.5` for ids 1, 3, 5 |
|---|---|---|
| default, `integer64` | exact | **2, 5, 8** |
| `bigint = "integer"` | **NA, with no warning** | 1.5, 4.5, 7.5 |
| `bigint = "numeric"` | exact | 1.5, 4.5, 7.5 |

The default multiplies a non-integer and rounds the answer back to an integer64
without a word, and `is.numeric()` still returns TRUE, so an ordinary type check
waves it through. Division is fine: `sum(claim_nb) / count(*)` comes back as a
correct double, which is why this does not show up in the first aggregate
anyone writes.

The first probe tested only the default against `integer`, and `integer` looked
like the fix. Testing what it does with a value that does not fit found the
silent NA. `numeric` is exact to 2^53, nothing in this project is within nine
orders of magnitude of that, and it is what `R/db.R` uses. It matters beyond the
one `bigint` column in the frame: Postgres returns `bigint` for `count(*)` and
for `sum()` over integers, so every ad-hoc aggregate goes through this setting.

## 2026-09-12 · The upstream `1e+05` looks like an R export, and we nearly made our own

`numeric` has its own trap. With `idpol` as a double, `as.character(100000)`,
`paste(100000)` and `format(100000)` all return `"1e+05"`. `write.csv` writes
`99999`, `1e+05`, `100001`: only the round number changes form.

That is exactly the malformed id repaired in transform 002. So checked whether it
explains it. R switches to scientific notation when that is no longer than fixed
notation, so `100000` becomes `1e+05` but `2100000` stays fixed, because
`2.1e+06` is just as long. Every multiple of 100,000 in the data:

| id | written upstream | `write.csv` from a double |
|---|---|---|
| 100000 | `1e+05` | `1e+05` |
| 2100000 | `2100000` | `2100000` |
| 3200000 | `3200000` | `3200000` |
| 5100000 | `5100000` | `5100000` |

Four for four, and no id that R would abbreviate appears upstream in fixed
notation. CASdatasets is an R package. The most likely origin of the bad id is
someone writing a double `idpol` column to CSV from R. That is an inference, not
a proof, but it is the only explanation checked against every case the data
offers.

The practical point is closer to home. The fingerprint script originally
formatted `idpol` with `%.0f` precisely to avoid this, and any later R code that
pasted an id into a file name or a log line would have reproduced the upstream
defect in this project's own output. So the loader now converts `idpol` to an
R integer, after asserting every value fits in int32 with nothing lost, and the
reason is written next to the conversion.

## 2026-09-12 · Codes, not keys, and one exposure column

Two things are left out of the frame on purpose, and both are guards against a
formula that runs without complaint.

**Surrogate keys.** `region_key` is an integer from 1 to 22. In a model formula
an integer is a continuous covariate. Measured on one row per region:
`model.matrix(~ region_key)` has 2 columns, an intercept and a single slope
across alphabetically numbered regions, while `model.matrix(~ region)` has 22.
Neither raises a warning. The frame carries region, area and vehicle as codes, so
the one-slope version cannot be written against it.

**The uncapped exposure.** Carrying `exposure_raw` beside `exposure` would put
`offset(log(exposure_raw))` one typo away. The frame has one exposure column,
the modelling one; the raw value stays in `fact.exposure` for anyone who wants
it on purpose.

On the Python side the matching trap was the column type. Postgres `numeric`
arrives through psycopg as `decimal.Decimal`, and pandas stores a column of
Decimals as `object` dtype. The view casts exposure to `double precision`, so
both languages receive a float and neither has a conversion to remember. Postgres
writes float8 as the shortest text that round-trips, so both parse it to the
identical double, which is what lets the md5 match.

Load time, connect through validated frame, three runs each: R about 1.4 s,
Python about 2.2 s. The first Python measurement read 5.8 s because it timed the
hashing as well; the spans were aligned before comparing.

---

## 2026-09-13 · A weight is not the error. A weight on the count is.

The classic mistake with exposure in a frequency GLM is usually stated as "used
exposure as a weight instead of an offset". Fitted it every way it can be written,
on the same frame and the same 38 rating parameters, and that statement turned out
to be half wrong.

With the *claim rate* as the response and exposure as a prior weight,
`glm(claim_nb / exposure ~ ..., weights = exposure)` matches the offset model
coefficient for coefficient, to within 5.3e-08. It is the same model. The error is
keeping the *count* as the response and adding the weight. That fits claims per
policy rather than claims per policy-year, and it runs without a warning:

| | Offset | Weight on the count |
|---|---|---|
| Exposure-weighted annual frequency | 0.1007, observed 0.1007 | 0.0647, 36% low |
| Regular fuel against diesel | 1.053 | 0.973 |
| Brand B12 against B1 | 1.167 | 0.769 |

The fuel effect changes sides. A rate table built from that fit would give a
discount where the data says to load, and nothing in the fitting output hints at
it. The fit even looks better by the usual numbers, deviance 126,893 against
217,287, but prior weights change the likelihood being maximised, so its deviance
and AIC are not comparable with the unweighted fits at all. The unweighted fits
share a response and a likelihood and can be compared with each other; the
weighted one can only be compared on something every specification implies, which
is why the document leads with the annual frequency each one predicts.

`R/frequency.R` now holds the only frequency fit in the project. It always uses
the offset, and it checks the balance property that proves it: with a log link,
an intercept and an offset, fitted claims must sum to observed claims, 36,102.
A weight on the count cannot pass that check.

## 2026-09-13 · The data rejects proportionality everywhere, not just at the short end

D1-4 measured claims per policy-year falling ninety-fold from the shortest
exposure band to the longest, and concluded the offset's assumption failed "at
the short end". Estimating the coefficient on log(exposure) instead of fixing it
at 1 tests that directly:

| Fit | Coefficient | 95% interval | Standard errors below 1 |
|---|---|---|---|
| all policy-years | 0.367 | [0.355, 0.379] | 103 |
| short exposure removed | 0.399 | [0.386, 0.412] | 90 |

Doubling a policy's exposure multiplies its expected claims by 1.29, not 2.
Inflating the standard errors by the Pearson dispersion leaves it 98 below 1.
Removing the 13,603 short-exposure rows barely moves it. The departure from
proportionality runs through the whole range of exposure, and the rows D1-4
flagged are not its cause.

The offset is still the pricing model, and that is a decision rather than an
oversight. Exposure here is the fraction of a year a policy was in force, and a
policy can stop being in force because it claimed. The data has no dates or
cancellation reasons, so that cannot be confirmed, but if it happens then exposure
is partly an outcome, and a model that estimates its coefficient is partly
learning from the outcome, which would also go a long way to explaining why one
extra parameter buys 8,157 of deviance. At the moment a policy is priced, its eventual exposure
is not known, and the rate has to be for a full year. So estimated exposure is a
diagnostic of the offset's assumption, recorded with its size, and not a rating
input. It is also not carried into D3 as a competing model, because scoring a
holdout with each policy's realised exposure would leak the outcome into the
comparison.

## 2026-09-13 · Correcting D1-4: the short-exposure rows are high-residual, not high-leverage

D1-4 described the 13,603 policies exposed for under a week, 362 claims on 0.03%
of exposure, as "the precise shape of a high-leverage point", and warned that
removing them "would improve every diagnostic while changing the answer". The
first half is right. The second is wrong, and now measured:

| Offset model | All rows | Short exposure removed |
|---|---|---|
| Pearson dispersion | 2.648 | 1.951 |
| Largest change in a key relativity | | 0.45%, regular fuel |
| Largest change in any coefficient | | 3.8%, region R43 |

The reason is how a Poisson fit weights rows. The IRLS working weight under a log
link is the fitted mean, and under an offset the fitted mean is proportional to
exposure. A one-week policy carries about a fiftieth of the weight of a full-year
policy with the same risk. Its residual can be enormous, 362 observed claims in
the band against 12 fitted, and it still has almost no say in the coefficients.

The live comments that repeated the claim, in transform 003 and in a test
docstring, have been corrected in place. The D1-4 entry above is left as written,
because this log records what was believed when, and a quietly edited entry would
make it look as if the mistake was never made.

## Open for D2-4: the dispersion depends on the exposure specification

The plan for D2-4 is to compute the Pearson dispersion of the frequency model and
report whether it is overdispersed. The same data gives very different answers
depending on how exposure enters:

| Specification | Pearson dispersion |
|---|---|
| offset | 2.648 |
| offset, short exposure removed | 1.951 |
| log(exposure) estimated | 1.111 |

Most of the apparent overdispersion under the offset is the misfit of the exposure
relationship, not extra-Poisson variation between policies. Computing the
statistic on the offset model alone would report strong overdispersion and
attribute it to the wrong thing. For the offset model's own standard errors the
2.648 is still the right correction; what it means is the question D2-4 has to
answer honestly.

## 2026-09-13 · A generated document has to be checked for staleness two ways

`docs/frequency-exposure.md` is written by `R/frequency_exposure.R`, eight fits
and about ninety seconds, too slow for the test suite. So the document carries what
it was computed from, and tests check it has not drifted:

- the md5 of the model frame, which changes if the mart is rebuilt differently
- the rating terms, which change if `FREQUENCY_TERMS` in `R/frequency.R` does

Checking only the md5 would miss the second: change the model formula, forget to
regenerate, and the committed document describes a model that no longer exists
while every data check still passes. The script also writes nothing that varies
between runs, no timestamp and no runtime, so regenerating on unchanged data is a
byte-for-byte no-op. The first draft printed its runtime at the bottom, which
would have made every regeneration look like a change.

Two figures from the exploration did not survive into the document, and are
worth naming so they are not repeated. A probe reported the weight-on-count error
as 39% low; that compared unweighted averages of per-policy predictions, which is
not the quantity a rate balances on, and on the exposure-weighted basis the
figure is 36%. And a draft of the document said a "correctly specified" rate
balances to observed frequency. Balance is a property of the proportional model,
not of correctness, and the text now says so.

## 2026-09-13 · Thirteen files had CRLF endings, and the diff was lying about how much changed

Staging this commit showed `sql/transform/003_adjusted.sql` as 131 lines added
and 122 removed, for an edit to two comments. With `--ignore-cr-at-eol` it was 13
and 4. The rest was line endings.

The first attempt to survey the repository got the answer wrong. Counting lines
that end in a carriage return with `grep` in Git Bash reported all 43 tracked
files as CRLF, because grep on Windows treats the carriage return specially.
Reading the bytes with `od` showed `R/model_frame.R` ending in a bare `\n` in
HEAD. A byte-level count in Python gave the real picture: 30 files LF and 13 CRLF
in HEAD, and in the working tree `DEVLOG.md` had 740 LF lines and 133 CRLF lines
in the same file.

The 13 were exactly the files edited in earlier commits through
`pathlib.Path.write_text`, which on Windows translates every newline to CRLF on
write. Nothing else in the toolchain does that, so the repository had been
accumulating a second convention one edit at a time.

It was worth more than a cosmetic fix because `scripts/migrate.py` hashes each
migration's bytes. The preceding commit converted migration 005 to LF, and the
existing database, built from the CRLF version, was then refused exactly as
predicted: `refusing to migrate: these files were edited after being applied:
005_load_and_indexes.sql`, exit 1. Same migration, different bytes.

The fix went in as its own commit, built directly in the index from HEAD's blobs
so the working tree holding this commit's changes was not touched, and it changes
nothing but line endings and a new `.gitattributes` setting `* text=auto eol=lf`.
That normalizes on add and overrides `core.autocrlf` on checkout, which is set to
`true` in this machine's system Git config. This commit's twelve files were
checked byte for byte against a backup taken before any of it, and are identical
apart from their endings.

Edits from Python now write bytes, or pass `newline="\n"`.

---

## 2026-09-13 · Equal-exposure deciles put the band boundaries in the wrong place

The plan was to band driver age, vehicle age and bonus-malus at equal-exposure
deciles. Before banding anything, checked where the linear terms of D2-2 actually
fail, with one-way actual over expected claims. Scored on the holdout, from
`docs/frequency-banding.md`:

| Group | Holdout claims | Actual over expected, linear terms |
|---|---|---|
| driver age 19 | 47 | 1.626 |
| driver age 20 | 72 | 1.434 |
| driver age 31 to 35 | 664 | 0.817 |
| driver age 46 to 50 | 953 | 1.186 |
| vehicle age 0 | 1,014 | 2.172 |
| vehicle age 1 | 607 | 0.690 |

Risk changes fastest at the youngest drivers and at new vehicles, and those are
exactly the places an equal-exposure decile cannot see. Drivers under 21 hold
0.72% of exposure, so the first driver decile runs from 18 to 29, putting an
18-year-old in the same band as a 29-year-old. The first vehicle decile merges
age 0 with age 1. The method spends its resolution where the exposure is, and the
risk gradient is somewhere else.

So a second banding was built on the risk structure instead: two- and three-year
bands at the young end, five-year bands through the middle, vehicle age 0 on its
own, and bonus-malus 50 and 100 on their own as states of the French scale. It was
kept to 66 parameters against the deciles' 63, so the comparison would be about
where the boundaries sit rather than how many there are.

On a holdout, scored out of sample:

| Specification | Parameters | Holdout deviance | Against linear |
|---|---|---|---|
| linear | 38 | 43,596.8 | |
| equal-exposure deciles | 63 | 43,165.8 | -431 |
| risk-structured bands | 66 | 42,707.9 | -889 |
| risk-structured, vehicle power as a factor | 76 | 42,648.8 | -948 |

The deciles recover less than half of what the same number of parameters buys when
the boundaries follow the risk. And out of sample they still fail in exactly the
predicted places: vehicle age 0 reads 1.84 and vehicle age 1 reads 0.56, because
the band that merges them fits neither.

## 2026-09-13 · The rule came first, and it picked the specification with vehicle power

The choice between specifications was written into the script before any fit ran:
lowest Poisson deviance on the holdout wins, and BIC on the full data is reported
as a check rather than a second vote. Adding vehicle power as a factor won by 59 of
holdout deviance, 0.14%, which is small. BIC picked the same specification, so the
rule and the check agree, and the small margin is recorded rather than explained
away.

Two things the chosen model still does not do well, both visible in the holdout
tables and both left alone. Deciding to fix them after seeing the holdout would be
choosing bands to fit the test set.

- Driver age 31 to 35 reads 0.915 and 41 to 45 reads 1.077, each a little over
  two standard errors from 1 on 664 and 927 holdout claims. The mid-age hump is
  only partly captured.
- Vehicle power 12 to 15 has 81, 32, 30 and 29 holdout claims, and a separate
  factor level for each fits them noisily: 14 reads 1.36. Shrinking thin levels
  towards their neighbours is what credibility weighting is for, and that is D4-4.

## 2026-09-13 · A column of 1.000s that meant nothing

The first run of the banding document printed actual over expected for the chosen
specification, and every vehicle-age, bonus-malus and vehicle-power row read
exactly 1.000. It looked like a perfect fit.

It was arithmetic. A Poisson GLM with a log link has one score equation per
dummy variable, and each one forces fitted claims to equal observed claims within
that level. The diagnostic groups had been drawn to match the bands, so in sample
they were bound to read 1 however good or bad the model was. Only the driver-age
table carried information, because its groups were finer than the bands, and it
showed 1.149 for 18-year-olds.

Every actual-over-expected table now comes from the holdout: fitted on training
risk groups, scored on the rest, with the holdout claim count beside each row so
the noise can be judged. Density is the one exception, read in sample, because it
is a continuous slope with no levels to reproduce, and the document says why.

## 2026-09-13 · 14% of rows look like pieces of one policy

A holdout is only honest if nothing in it has also been seen in training. The
policy id is unique in this table, so splitting by id looked safe. Checked anyway,
by comparing each row with the row holding the previous policy id:

```sql
-- rows whose nine rating factors equal those of the previous policy id
lag(ROW(area, veh_power, veh_age, driv_age, bonus_malus,
        veh_brand, veh_gas, density, region)::text) OVER (ORDER BY idpol)
```

96,478 rows, 14.2%, match the row before them on all nine factors, down to the
exact population density. Consecutive ids agreeing on everything is not chance.
It is what one policy recorded as several rows looks like. A split on the id would
put different pieces of the same policy on both sides.

The holdout therefore splits by risk group, a run of consecutive ids with identical
rating factors, every fifth group held out: 116,307 groups and 135,455 rows. In the
banding document, 181,795 rows sit in a group of two or more.

Two consequences reach beyond this commit.

**D3-2 has a different problem from the one planned.** The plan's pitfall is a
random row split scattering one policy's several rows across train and test, which
presupposes repeated policy ids. There are none. The leak that does exist is the
fragments, which share no id at all, and a split by id would walk straight into it.
For a gradient boosting model, which can memorise a profile, this is the split that
matters.

**It may bear on the D2-2 finding.** If one policy-year is cut into several short
records and a claim lands in only one of them, those fragments would look exactly
like short exposures carrying too many claims. Some of the non-proportionality
could be the fragmentation rather than the policies. That is a hypothesis, not a
result: re-estimating the exposure coefficient on reassembled groups would test it,
and it has not been done.

## 2026-09-13 · Vehicle age 0: 43% of the relativity goes with short exposure

New vehicles carry the largest relativity in the book, and the shortest exposures,
a mean of 0.289 of a year against 0.551. On all policy-years the model's band-0
relativity against band 1 is 3.43. Refitted on exposures of half a year or more it
is 2.02. On the log scale 43% of the new-car effect disappears once short exposures
are set aside.

The pricing model is fitted on all policy-years and carries 3.43. Whether a new car
should pay that or something nearer 2 is a rate-table judgement, and it goes to D4
recorded as the single relativity most exposed to the non-proportionality D2-2
measured. Driver age does not have the same problem. The raw frequency of drivers
aged 18 to 20 against everyone older is 2.49 on all policy-years and 2.16 on
exposures of half a year or more, a much smaller shift.

## 2026-09-13 · A data-masking bug that one check happened to catch

The first draft of `apply_bands()` took its bound vectors as arguments named
`veh_age` and `bonus_malus`, and called `band_of(veh_age, veh_age)` inside
`dplyr::mutate`. Inside mutate a bare name resolves to a data column before a
function argument, so the call received the column twice.

Tested what that actually does rather than guessing. On three rows that happened to
be sorted, it returned vehicle-age bands of 0-3, 4-19 and 20+, built from the data
itself, without any error. On the real frame, where the column is unsorted,
`band_of()`'s check that bounds are strictly increasing stopped it. The check was
written for a different reason and was the only thing between the bug and silently
wrong bands. The arguments are now named `*_lower` and referred to through `.env`,
and `check_bands()` verifies every band's observed range against its label.

## 2026-09-13 · The exposure analysis keeps its own terms

`R/frequency_exposure.R` took its rating terms from `FREQUENCY_TERMS`, which now
holds banded terms. Its relativity table reports per-year slopes for driver age and
vehicle age, which banded terms do not have, so it would have failed, and its
conclusions belong to the linear specification it was run on. It now pins
`EXPOSURE_ANALYSIS_TERMS` to that specification, and its staleness test reads that
constant. On the banded terms the exposure coefficient is 0.411 against 0.367, so
the D2-2 conclusion survives the change of specification.

Re-running the exposure analysis after the change produced a document byte for byte
identical to the one committed in D2-2, which is the evidence that pinning the
terms changed nothing it computes. The banding analysis was also run twice on the
same frame and produced identical documents both times.

---

## 2026-09-13 · Two dispersion estimators, read against 1, point in opposite directions

The plan for this commit was to compute the Pearson dispersion of the frequency
model, report it, and mark the Poisson standard errors as understated. On the
banded pricing model the textbook reading of the two textbook estimators is:

| Estimator | Value | Read against 1 |
|---|---|---|
| Pearson chi-square / df | 2.375 | overdispersed |
| deviance / df | 0.313 | underdispersed |

Both cannot be right, and neither is. With a mean of about 0.05 claims per row,
neither statistic has 1 as its reference value. So before computing either, the
rule was written into the script: judge each statistic against its own distribution
under the fitted Poisson model, from 100 simulated claim vectors, not against 1.

| Estimator | Observed | Simulated range when Poisson is true |
|---|---|---|
| Pearson / df | 2.375 | 0.977 to 1.025 |
| deviance / df | 0.313 | 0.279 to 0.283 |
| sum of (y - mu)^2 / sum of mu | 1.070 | 0.988 to 1.013 |

Against the simulation all three agree: every observed value is above every
simulated one, and the Poisson variance does not hold. The deviance statistic that
reads as underdispersion is in fact evidence of the opposite, because a correct
model would put it at 0.281. Anyone comparing it with 1 would have concluded the
standard errors were too wide.

## 2026-09-13 · Nearly half the Pearson statistic comes from 0.1% of rows

Agreeing that the variance is wrong is not the same as agreeing how wrong. The
Pearson statistic implies 138% excess variance and the mean-weighted ratio 7%.

The Pearson statistic averages (y - mu)^2 / mu over rows, and a row where the model
expects 0.00215 claims and sees one contributes about 460 by itself.

| Largest contributions | Share of the Pearson sum |
|---|---|
| 68 rows, 0.01% | 19.0% |
| 678 rows, 0.1% | 43.6% |
| 6,780 rows, 1% | 76.1% |

The 678 rows at the top have a median exposure of 0.02 of a year and a median
fitted claim count of 0.00215, and 542 of them had exactly one claim. They are the
exposure misfit D2-2 measured, turning up as variance. Without them the Pearson
dispersion is 1.34. The ratio of summed squared residuals to summed fitted claims,
which weights rows by their fitted mean and so cannot be dominated by tiny-mean
rows, reads 1.070: about 7% excess variance across the bulk of the book.

## 2026-09-13 · The standard errors are understated by 8%, not 54%

What matters for inference is the uncertainty in the coefficients, not a dispersion
number, so it was measured directly.

| Standard errors | Median against Poisson | Largest | Significant at 5% |
|---|---|---|---|
| Poisson | 1.000 | | 51 of 76 |
| quasi-Poisson, scaled by the Pearson dispersion | 1.541 | 1.541 | 43 of 76 |
| sandwich, rows independent | 1.023 | 1.172 | 51 of 76 |
| sandwich, clustered by risk group | 1.084 | 1.258 | 50 of 76 |

The pitfall as usually stated is that an unchecked Poisson model understates
standard errors and makes significance look better than it is. That is true here,
by about 8%. The usual remedy, quasi-Poisson, would widen every standard error by
54% and remove eight coefficients from significance on the strength of a statistic
nearly half made of 678 rows with a median exposure of about a week. Over-correcting
is its own error.

The sandwich estimator was written by hand in `frequency_vcov()`, so it was checked
where the answer is known: refitted on claims simulated from the fitted model, it
reproduces the Poisson standard errors, median ratio 1.000 both with rows
independent and clustered. The generating script refuses to write the document if
that check drifts more than 3%.

Clustering by risk group matters, and it matters because of D2-3. Pieces of the
same policy are not independent, and allowing for it adds about 6% on top of the
row-level sandwich, most for regions R91, R21 and R94 and for vehicle age band 1.
Every confidence interval and significance statement about the frequency model now
comes from `frequency_vcov(fit, cluster = risk_groups(frame))`.

`risk_groups()` moved into `R/model_frame.R` in this commit, because the banding
holdout and the clustered errors now both depend on it and D3-2 will too. It refuses
a frame not in increasing policy-id order, since consecutive ids only mean something
in that order. The banding document regenerated byte-identical after the move.

## 2026-09-13 · The fragments explain 12% of the non-proportionality

D2-3 left a hypothesis open: pieces of one policy-year recorded as separate short
rows could produce the non-proportionality D2-2 found. Tested by reassembling every
risk group into one row, summing claims and exposure.

678,013 rows become 581,535 groups, 85,317 of them made of two or more pieces. Only
102 reassembled groups exceed a year of exposure, which is what pieces of a single
policy-year would do and is the strongest evidence so far that the groups are what
D2-3 took them to be.

| Estimated coefficient on log(exposure) | Coefficient | Std. error |
|---|---|---|
| policy-years as recorded | 0.411 | 0.0063 |
| risk groups reassembled | 0.481 | 0.0075 |

Reassembly moves the coefficient towards 1 and closes 12% of the distance. The
hypothesis was partly right and mostly not: the fragmentation is real and accounts
for some of the effect, and the rest is still unexplained by anything in this data.
The Pearson dispersion of the offset model falls from 2.375 to 2.067 on the
reassembled groups, consistent with the same reading.

## 2026-09-13 · What was not done, and two script bugs caught before the first run

No negative binomial model was fitted. The plan cut it, and the evidence does not
ask for it: the variance problem is concentrated in a small set of short-exposure
rows, with about 7% excess elsewhere, and a negative binomial would reweight every
row and move the
point estimates to answer a question the sandwich estimator already answers without
choosing a variance function.

The generating script did not parse on its first attempt. Two tables had an extra
closing parenthesis, left there when the script was written and found only because
R refused to parse it before the several-minute run began. And a first draft wrote
"strongly overdispersed" and "above every simulation" into the document as fixed
text; both are now computed from the numbers, and the script stops if the
estimators ever stop pointing in opposite directions against 1, or if clustered
errors stop being smaller than the quasi-Poisson rescaling, since the conclusion
rests on both.

---

## 2026-09-13 · Does a zero-claim policy break a Gamma fit, or vanish from it? Both

The planned pitfall for this commit is fitting severity on zero-claim policies: a
Gamma model cannot take a zero, and the mistake either errors or silently drops
rows. Which one it does turned out to depend on a detail that looks cosmetic, whether
a policy's missing amount was written as 0 or left missing.

| Response built from | Outcome |
|---|---|
| all policies, missing amounts as 0 | fails: non-positive values not allowed |
| all policies, missing amounts left missing | runs; 653,069 of 678,013 rows silently dropped |
| `claim_nb > 0` only, missing as 0 | fails |
| `claim_nb > 0` only, missing left missing | runs; 9,116 of 34,060 rows silently dropped |

Written as 0, every version fails. Left missing, every version runs, because
`glm()`'s default `na.action` removes rows with a missing response before fitting
and says nothing. The silent versions all end up on the same 24,944 policies, which
is the correct population, by accident.

The danger in the silent path is not a wrong fit here. It is what the fit never
reports: restricting to `claim_nb > 0`, which is the standard advice, still handed it
9,116 policies whose reported claims have no amount, and they disappeared without a
line of output. That is D1-3's 26.7% of claims, and it is the finding the rest of
this entry is about.

Severity is now fitted only through `fit_severity()`, on `model.severity_frame`, a
view with one row per priced claim that takes its rating factors from the frequency
frame so both models see identical values. It refuses a missing amount and a
non-positive one by name, passes `na.fail` to `glm()` so nothing can be dropped
quietly, and refuses a fit that used fewer claims than it was given. A test runs
those guards in R on synthetic claims; with the missing-amount check removed, it
fails.

## 2026-09-13 · Reported claims times severity is 36.5% above recorded losses, and the data cannot say which is right

Frequency is fitted on 36,102 reported claims. Severity can only be fitted on the
26,444 that have amounts. Multiplying the two:

| Pure premium per policy-year | Value |
|---|---|
| recorded losses over exposure | 167.18 |
| reported-claim frequency times mean severity | 228.23, 36.5% high |

The first draft of the analysis added a third row, priced-claim frequency times mean
severity, reproducing 167.18 exactly, and called it the version that "reproduces
recorded losses". It does, by definition: priced claims times their mean amount is
recorded losses. It is an identity and was not evidence of anything. The document
now says so, and the 36.5% is what it actually is, the ratio of reported to priced
claims.

Whether that is an overstatement depends on what an unpriced claim is, and nothing in
this data says. If unpriced claims cost nothing, closed without payment, a frequency
model of reported claims overprices the book by 36.5%. If their amounts are simply
missing from the file, recorded losses understate the true cost instead.

What the data can say is that they are not a random subset:

| Claim-reporting policies | Some claims unpriced | All claims priced |
|---|---|---|
| mean vehicle age | 4.1 | 7.4 |
| mean exposure, years | 0.505 | 0.692 |
| mean driver age | 49.3 | 45.1 |

By region, the share with an unpriced claim runs from 16% in R82 to 53% in R21. So no
single scaling factor corrects for them, and the claim count the frequency side of
pure premium uses will move relativities, not only the level. That choice belongs to
D3-1, where pure premium is assembled, and it goes there with this evidence rather
than a guess about what the file's authors meant.

## 2026-09-13 · A Gamma log link balances ratios, not totals

The frequency model's fitted claims sum exactly to observed claims, because a Poisson
log link's intercept equation forces it. A Gamma log link's intercept equation forces
something else: the mean of amount over fitted amount, which comes out at 1.0000.
Fitted severity summed over all claims is 97.17% of recorded losses. Pure premium
built on this fit inherits a level 2.8% low unless it is rebalanced, and that decision
goes to D3-1 with the claim-count one.

## 2026-09-13 · glm() stops at 25 iterations and returns the coefficients anyway

On all 26,444 claims the Gamma fit converges in 24 iterations, inside `glm()`'s
default limit of 25. On the five training folds it needs 27, 28, 13, 27 and 25, so
three of the five would stop before converging. When that happens `glm()` returns
coefficients with a warning, which in a loop over folds is easy to miss.
`fit_severity()` allows 100 and refuses any fit that has not converged.

## 2026-09-13 · One claim decided a model comparison

A rule was fixed before fitting: on the frequency holdout's risk groups, compare the
76 frequency terms against a constant, and the lower Gamma deviance wins. The
constant won, by 194.0. Before accepting that, the 194 was broken down claim by claim.

| Fold-0 holdout, rated minus constant | Deviance |
|---|---|
| all 5,311 claims | 194.0 |
| one claim of 390,742 | 186.1 |
| every other claim | 7.9 |
| claims at or below the median amount | -533.9 |
| claims above the 99th percentile | 435.1 |

The rated model predicts the bulk of claims better and a few enormous ones worse, and
one claim is 96% of the margin. Repeated over all five folds, the difference is 194,
-105, -1,963, 458 and -878. It changes sign, and its size is set by which large claims
a fold happens to hold.

The rule's verdict was not taken. That needs saying carefully, because declining a
pre-committed rule after seeing its answer is exactly the move a pre-committed rule
exists to prevent. The reason is not that the answer was unwelcome. It is that the
decomposition shows the evaluation cannot discriminate between the models on
uncapped amounts, in either direction, so neither answer would mean anything. The
specification is left undecided rather than re-picked under a new rule: `fit_severity()`
has no default terms, and D2-6 chooses them after large losses are capped, with its
candidates and a multi-fold rule written down first.

D2-6's choice will not be blind, and the document records why: while this analysis
was being written, the single-fold comparison was also run on claims capped at the
99.5th percentile, and the rated model came out slightly ahead.

## 2026-09-13 · Rscript crashes on a multi-line -e argument

The test for `fit_severity()`'s guards first passed its R code to `Rscript -e`. On
this Windows machine that exits with status 3221225477, an access violation, and no
output at all, so the fixture looked like R had failed to start. The test writes the
script to a temporary file and runs that instead.

---

## 2026-09-13 · Cap large claims, do not drop them

The plan said to truncate claims above the 99.5th percentile. Truncating can mean
two things, and they cost very different amounts. Dropping a claim above the
threshold throws away the part of it below the threshold as well; capping keeps
every claim and sets aside only what lies above.

| Threshold | Cap | Claims above | Capped off | Lost if dropped |
|---|---|---|---|---|
| 99th percentile | 16,451 | 265 | 30.7% | 38.0% |
| 99.5th percentile | 34,377 | 133 | 25.3% | 33.0% |
| 99.9th percentile | 152,223 | 27 | 14.8% | 21.6% |

At the planned threshold, dropping would leave pure premium 33% below recorded
losses. Capping sets aside 25.3%, which is added back as a load of 1.3391 on capped
severity. `cap_claims()` replaces the amount with the capped one and keeps the
original beside it, so a frame shows whether it has been capped and refuses to be
capped twice.

A quarter of all losses above a cap is not a small simplification, and the load
rests on very few claims: the single largest supplies 26.6% of everything capped
off, and without it the load would be 1.2490. The cap is fixed as a constant, like
the rating bands. Even its value depends on a definition: R's nine quantile types
give 34,375 to 34,739 on this data, so the constant records type 7, which matches
Postgres `percentile_cont`, and a test recomputes it there.

## 2026-09-13 · Capping made the severity comparison decidable

D2-5 handed on two candidates, a constant and the frequency terms, and the promise
that the rule would be written down before fitting. It was, in the header of
`R/severity_large_losses.R`: lower total out-of-fold Gamma deviance over the five
risk-group folds, on capped amounts. No third candidate was added, because anything
added now would have been shaped by D2-5's drop-one tests.

| Rated minus constant, by fold | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| uncapped, from D2-5 | 194.0 | -105.1 | -1,962.7 | 458.2 | -878.0 |
| capped | -21.2 | 42.0 | -17.8 | -11.4 | -32.7 |

Capped, the frequency terms win by 41.0 in total and in four folds of five. The
margin is small, 0.13%, and it is recorded as small. On all claims, capping also
changes both of D2-5's side findings: the fit converges in 7 iterations instead of
24, and fitted amounts sum to 0.9999 of capped losses instead of 0.9717 of uncapped
ones. Iteration counts on the capped training folds were not recorded.

As D2-5 disclosed, this was not a blind choice: a single-fold capped comparison had
already been seen. The five-fold result was first computed by the committed script.

## 2026-09-13 · A flat large-loss load survives its test, narrowly

Loading the capped-off losses back as one factor assumes large claims fall evenly
across the book. The rule, also fixed in advance, was to reject that if the
probability of a claim exceeding the cap rose with predicted severity. The slope of
that probability on log out-of-fold predicted capped severity is 1.016, with a 95%
interval of -0.031 to 2.062.

The interval includes zero, so the rule does not reject a flat load and it is kept.
But the point estimate doubles the odds of a large claim for each doubling of
predicted severity, and with 133 large claims the test could easily miss an effect
of that size. The document says so, and the flat load goes forward as a provisional
simplification for the rate table rather than as a finding that large claims are
spread evenly.

## 2026-09-13 · The residual plot showed the opposite of what the text already said

The diagnostics use randomised quantile residuals, which are standard normal under
a correct model whatever the distribution. Ordinary residuals of a Poisson model on
mostly zeros plot as stripes, and a capped amount is not Gamma at the cap at all.

For frequency the plot says what the numbers already did. Policy-years under 0.1 of
a year put 6.62% of residuals beyond ±1.96 and lift off the line in the upper tail;
longer ones put 5.14% there and follow it. That is D2-2's non-proportionality, seen
as a shape.

For severity, the first draft of the generating script contained its conclusion
before it had been run: capping "brings the tail back towards the line", which would
make "a Gamma model on capped claims plus a load" a defensible pair. The numbers came
back 7.03 for the 99.9th percentile of the residuals uncapped and 6.40 capped,
against a normal 3.09. The figure showed both panels S-shaped and nearly identical.
The paragraph was wrong and was deleted, and the text was rewritten from the figure.

The S has a plain cause, found by counting exact amounts:

| Claim amount | Share of claims |
|---|---|
| 1,204.00 | 18.1% |
| 1,128.12 | 11.6% |
| 1,172.00 | 7.8% |
| 1,128.00 | 3.1% |

Four exact amounts hold 40.7% of all claims, and 602 is exactly half of 1,204. They
look like standard settlement figures rather than individually assessed losses; the
data does not say where they come from. No continuous distribution fitted across the
whole range can put that much mass on four points, so the middle of the plot is flat
whether the tail is capped or not. It may also be part of why rating factors say so
little about severity: two claims in very different risks can both be settled at
1,204.

The practical conclusion is narrower than the one the draft wanted to reach. A Gamma
GLM estimates the mean consistently when the mean is right, whatever the true
distribution, and pure premium needs only the mean, which capping has made stable
and balanced. Nothing should be taken from the fitted distribution: not tail
probabilities, not simulated claims, not the price of a limit or an excess.

The figures are committed under `docs/figures/`. R's png device wrote identical bytes
for identical input across separate sessions on this machine, so, with the
randomised residuals seeded, regenerating produces no diff; the document and both
PNGs were checked across two runs.

---

## 2026-09-14 · The largest relativity in the book was made of claims with no amount

D2-5 left a choice for this commit: fit the frequency side of pure premium on the
36,102 reported claims or on the 26,444 that carry an amount. It was decided by
definition, before fitting anything: pure premium predicts recorded losses, the only
cost in the data, and every validation metric in D3 compares with them, so frequency
counts priced claims. `fit_pricing_frequency()` fits the same terms on
`priced_claim_nb`, from a new view, `model.policy_loss`.

Then both versions were fitted, to see what the choice does.

| Frequency relativity | Reported claims | Priced claims |
|---|---|---|
| vehicle age 0 against 1 | 3.431 | 0.982 |
| regular fuel against diesel | 1.076 | 0.859 |

77.3% of the claims reported on new vehicles have no amount, against 16% to 20% in
every other vehicle-age group. The new-car effect, which D2-3 recorded as the largest
relativity in the book and traced partly to short exposures, disappears on claims
that carry an amount. Fuel reverses.

This is the most consequential number in the project so far, and the data cannot say
which side is right. If an unpriced claim cost nothing, a model of reported claims
charges new vehicles about three times too much. If unpriced claims are real losses
whose amounts are missing or not yet settled, recorded losses leave most of the
new-vehicle cost out and the priced model undercharges them by about as much. The
priced basis is used because it is the only one that can be checked against anything.
The vehicle-age bands were chosen on reported claims, with band 0 kept separate
because risk changed fastest there, and on priced claims it does not; that goes to
the rate table with both relativities.

## 2026-09-14 · Three units, and a rebalancing step that would have hidden three bugs

The plan for this commit said the danger is units: frequency is claims per
policy-year, severity is amount per claim, and their product is loss per policy-year,
and a mistake between them runs without an error. The three are now columns with
those names, `claims_per_year`, `capped_amount_per_claim` and `amount_per_year`, and
`scripts/pure_premium.py` checks each against the data before multiplying:

- exposure times `claims_per_year` must equal priced claims, to 1e-6. If R had
  exported `fitted()`, expected claims for the observed exposure, this fails.
- priced claims times `capped_amount_per_claim` must be within 1% of capped losses. A
  severity that already carries the large-loss load fails at 1.34.
- exposure times `amount_per_year` must be within 1% of recorded losses, and is then
  rebalanced to them exactly. The off-balance factor is 0.999863.

The 1% was fixed before the first run. The question behind it was what an automatic
rebalance does to a units error, so every tempting mistake was assembled, rebalanced
to recorded losses, and compared with the chosen basis:

| Basis | Total before rebalancing | After: exposure under 0.1 | After: vehicle age 0 |
|---|---|---|---|
| exposure left out of the loss sum | 2.060 | 8.46 | 1.69 |
| exposure applied twice | 0.744 | 0.09 | 0.71 |
| reported claims | 1.379 | 1.14 | 3.19 |
| large-loss load left out | 0.747 | 1.00 | 1.00 |

After rebalancing every total is right. A missing flat load is genuinely harmless,
because it is the same kind of factor as the rebalance. The other three move money
between policies, and a Gini or lift chart would score them as models, not flag them
as bugs. An off-balance step without a tolerance is the place where a units error goes
to hide; this one would refuse all four.

## 2026-09-14 · R hands predictions to Python through Postgres, bit for bit

The GLMs live in R and the validation in Python. Rather than refit in Python or pass
a CSV, `R/export_predictions.R` writes the two frequency runs, the shared severity
prediction and all coefficients to `model.glm_prediction` and `model.glm_coefficient`,
with the frame md5, cap and load in `model.glm_run`. It reads the predictions back and
stops unless the doubles are identical, so the COPY path is known to be lossless
rather than assumed.

`model.glm_prediction` has no foreign key to `fact.exposure`. Transform 004 truncates
that table on every rebuild, and a reference into it would make the rebuild fail.
Staleness is checked instead where it matters: `pure_premium()` recomputes the frame
md5 and refuses predictions fitted on a different frame, and a test proves it does.

Migration 008 was edited twice after it was first applied locally, to add the
coefficient table and to align a column, each time followed by `migrate.py --reset`
and a rebuild. The freeze applies from the commit onwards.

A test ties the export to D2-3: the reported-claims run's band 0 relativity, computed
from the stored coefficient, must equal the 3.43 in `docs/frequency-banding.md`.

## 2026-09-14 · A correlation of -0.96 that was arithmetic, and a sentence about the annual rate that was wrong

Exploring the unpriced claims by region gave a correlation of -0.96 across the 22
regions between their unpriced share and the ratio of the two frequency models, and
the first draft of the report used it as evidence. The script's assertion on it
stopped the first run: the report computed the ratio the other way up, so the sign was
wrong. Looking closer, the number was not evidence in either direction. A Poisson fit
balances claims within every level of a rating factor, so in-sample each model prices a
region at its own claim count, and computed from totals the correlation is exactly -1.
It was the unpriced share restated. The document now gives the regional range and says
the price columns are consequences of the share, not a second finding. The
relativities are the part that is estimated, with the other factors held fixed.

The draft also said the partial-year gap below leaves the annual rate unaffected
because a new policy is priced for a full year. It does not: the fitted rate pools
short and full policy-years, and full years run at 0.83 of their expected claims.
Rewritten before the document was generated from it.

## 2026-09-14 · Claims on short policy-years are larger, not only more frequent

Pro-rata expected loss against recorded experience, in-sample, by exposure:

| Exposure | Claims A/E | Capped losses A/E | Claims above the cap per 1,000 claims |
|---|---|---|---|
| under 0.1 | 1.94 | 2.85 | 12.9 |
| a full year | 0.83 | 0.70 | 3.0 |

D2-2 measured the extra claims. The extra cost per claim is new: capped losses on the
shortest policy-years are 2.85 times expected against 1.94 for their claims, and
claims above the cap are four times as common. A policy that ends because of a costly
claim, a write-off, would produce both, which is the explanation D2-2
proposed. There are still no dates or cancellation reasons to confirm it. Exposure is
not a rating factor, so unlike the vehicle-age and regional ratios these are not
balanced by construction. Calibration by exposure in D3-5 will show this gap for any
model that prices pro rata.

---

## 2026-09-14 · One place for figures

The repository was set up with an ignored top-level `figures/`, on the plan that
figures would be rebuilt rather than committed. D2-6 decided the opposite, because
R's png device writes identical bytes for identical input, and put the committed
figures beside the documents that embed them, in `docs/figures/`. The empty
top-level directory, its ignore rule and the README line describing it were left
behind and contradicted that. They are removed.

With the root rule gone, the D2-6 test that guards `docs/figures/` was checked by
adding a rule that ignores it. The test still passed. It asked `git check-ignore`,
which by default does not report a file that is already tracked, and both PNGs are
tracked, so it could never fail. It now passes `--no-index`, and also requires
`git ls-files` to know both figures. With the extra rule it fails, and with a figure
removed from the index it fails.

---

## 2026-09-14 · A split by IDpol is a split by row, so the planned fix would have fixed nothing

The plan's pitfall for this commit was a random row split scattering one policy's
several rows across training and holdout, fixed by splitting on IDpol. IDpol is unique
in this frame, so a split on it is a row split, and D2-3 already found the real
problem: 14% of rows are pieces of a policy-year recorded under consecutive ids with
identical rating factors.

Both splits are now one view, `model.holdout`, read by R and Python:

- `risk_group_holdout`: every fifth risk group, a run of consecutive ids with the same
  nine rating factors. It reproduces D2-3's holdout exactly, 116,307 groups and
  135,455 rows, and `R/export_predictions.R` stops unless its groups equal
  `risk_groups()` in R. A test rebuilds them in Python as well.
- `idpol_holdout`: a fifth of rows by an md5 hash of the id, the split the plan named.
  A test recomputes the hash in Python for every row.

Under the IDpol split, 29,930 held-out rows have another piece of their risk group in
training, 14.7% of held-out exposure. The risk-group split has none, and a test holds
it to that.

## 2026-09-14 · Pieces of one policy-year share their claim counts, not their amounts

Before measuring any leak, the question was what a piece in training could reveal
about a piece held out. If pieces were separate periods they would claim
independently. Expected counts use each piece's Poisson probability of a claim.

| Groups of two or more pieces where every piece has a claim | Observed | Expected if independent |
|---|---|---|
| reported claims | 1,828 | 105.3 |
| priced claims | 51 | 34.6 |

Reported counts are shared, 17 times as often as independence allows, and 1,707 of the
1,828 groups have no amount on any piece. It looks like one claim count written onto
every piece of a policy-year, with the amount, where there is one, on a single piece.

That changes D3-1's picture. Counting each such group's claims once removes 1,959 of
the 9,658 claims without an amount, 20.3%, and 1,344 of the 4,024 on new vehicles,
33.4%. New vehicles are fragmented more: 37.8% of their risk groups have two or more
pieces, against 13.0% for older vehicles. That part of the unpriced claims looks like a
repeated count, not a missing cost, which leans towards the priced basis D3-1 chose
without settling the rest.

## 2026-09-14 · The leak is real for ClaimNb and absent for priced claims

A GLM with 76 parameters cannot remember a policy, so it cannot show a leak. The
measurement uses a memoriser instead: the GLM's prediction times the
credibility-weighted actual over expected of training rows with exactly the same nine
factors, (claims + k) / (expected + k). Both splits are scored on the 27,106 rows they
both hold out, so outcomes are identical and only the training rows differ.

| Deviance handed to the memoriser by the IDpol split | k = 0.5 | k = 2 | k = 8 |
|---|---|---|---|
| reported claims | 273.1 | 114.7 | 35.4 |
| priced claims | -4.2 | -0.4 | 0.0 |

On reported claims that is 7% to 52% of everything the rating factors gain over a
constant on the same rows, 528.9. On the IDpol holdout, at k = 2, the gain is 17.59 per
1,000 rows with a piece in training against 1.49 on the rest, which is the repeated count
being read back. On priced claims there is nothing to read. So the pitfall the plan
predicted exists, and it lives in `ClaimNb`, the claim count this dataset provides. The pricing basis chosen in D3-1 happens to avoid it. The risk-group
split is used anyway: it was fixed in D2-3, before any of this was measured.

Not blind in one respect: the memoriser and its k values were first tried in a
prototype on the all-rows GLM predictions, which gave 272.7, 114.6 and 35.3. The table
above is from GLMs fitted on each split's own training rows, by the committed scripts.

## 2026-09-14 · Two sets of metrics on two holdouts measured one claim

The plan said to report metrics for both splits and read their difference as the size
of the leak. For the pricing GLM:

| Priced claims, own holdout | Risk-group split | IDpol split |
|---|---|---|
| frequency deviance against constant | 4.91% lower | 4.85% lower |
| capped losses, actual over expected | 1.005 | 1.006 |
| recorded losses, actual over expected | 0.977 | 1.244 |
| recorded, without the largest claim | 0.977 | 0.886 |

The largest claim, 4,075,401, fell in the IDpol holdout. Read split against split, one
claim would have passed for a leak worth 0.27 of recorded A/E, on a basis where there
is no leak at all. Different holdouts differ by what they drew, which is why the leak is
measured on shared rows, and why D3-5 will show capped losses beside recorded ones.

## 2026-09-14 · GLM runs fitted on training rows

`R/export_predictions.R` now writes six runs: priced and reported claims, each on all
rows and on the training rows of each split. Migration 009 adds `trained_on` to
`model.glm_run`. `pure_premium()` runs its unit checks and computes its off-balance
factor on the rows a run was fitted to, 0.999775 for the validation run
`priced_claims_risk_group_split`, and applies it to every row. Checked on all rows
instead, the claims balance fails at 0.9991, and a test asserts that it does, so the
training mask cannot quietly become every row. The D3-1 document regenerates
byte-identical on the changed code.

---

## 2026-09-14 · The benchmark gets the GLM's rows, cap, load and exposure basis, but not its bands

For the comparison to mean anything, LightGBM is trained on exactly what the validation GLM
saw: the 542,558 training rows of the risk-group split, claims capped at 34,377, and the
same flat load, 1.3467, from the same training claims. The target is capped loss divided
by exposure, with exposure as the weight. Under a compound Poisson model that has the same
likelihood as capped loss with an exposure offset, so a prediction is a capped amount per
policy-year, the unit D3-1 fixed. `benchmark_pure_premium()` checks it against capped
losses before multiplying, as `pure_premium()` does for the GLM.

The objective is Tweedie. That is not the Tweedie GLM the plan cut. That would have been a
third model; this is the loss function of the second. The variance power, 1.5, was not
tuned. A compound Poisson sum of Gamma claims with shape α is Tweedie with power
(α + 2) / (α + 1), and D2-6 estimated the capped shape at 0.996, which gives 1.501. A test
ties the constant to that document.

The plan said "the same bands". The trees get the same eight rating factors without the
bands. Bands are how a GLM becomes non-linear, and handing a tree the GLM's cut points
would impose them on it. Area is left out, as in the GLM.

## 2026-09-14 · Tweedie boosting satisfied its objective and failed the pure premium check

The first full run stopped in `pure_premium`: exposure times the benchmark's prediction
came to 0.9566 of capped losses on its own training rows, outside the 1% tolerance D3-1
set. That tolerance exists to catch units errors, so the first question was whether this
was one. It was not. The Tweedie score equation on those rows is 1.2e-05, satisfied, and
the total drifts as boosting proceeds:

| Rounds | Predicted over actual, training rows |
|---|---|
| 1 | 0.9927 |
| 50 | 0.9564 |
| 199, chosen | 0.9566 |

A diagnostic run before the fix, with 15 leaves and 1,000 rounds, showed the drift growing
with the variance power: totals of 0.957, 0.844 and 0.766 at powers 1.1, 1.5 and 1.9. A
Tweedie log-link fit balances residuals weighted by μ^(1−p), not the total. It is the same
property that left the Gamma GLM at 0.9717 of recorded losses in D2-5.

The tolerance was not widened. The benchmark carries an explicit balance correction,
1.0454, estimated on its training rows and stored in `model.benchmark_run`, and
`pure_premium` still checks the stored predictions. The document reproduces the refusal
from the uncorrected predictions rather than describing it. Migration 010 gained the
`balance_factor` column before being committed, followed by `migrate.py --reset` and a
rebuild.

## 2026-09-14 · Tuning chose the smallest trees in the grid

Sixteen combinations were fixed before fitting: 7, 15, 31 or 63 leaves and a minimum leaf of
100, 500, 2,000 or 5,000 rows. Learning rate was 0.05 and early stopping 200 rounds, on five
folds of the training rows assigned by risk group. The lowest out-of-fold Tweedie deviance
won, and the holdout was not touched. It chose 7 leaves, a minimum leaf of 100, and 199
rounds. The whole grid spans 0.29% of deviance, and deeper trees were steadily worse.

The winner is on the edge of the grid in both dimensions, so the grid does not bracket the
optimum. It was not widened after seeing that, and the document says so. Seven leaves and a
minimum leaf of 5,000 are 0.001% behind, so the minimum leaf is not well determined.

## 2026-09-14 · LightGBM reads back the repeated claim counts, and finds a small leak on capped losses

D3-2 measured the leak with a memoriser and handed on the claim that a boosted model fitted
to `ClaimNb` with a row split would read it back. This commit tested that with the chosen
configuration. It compared folds assigned by risk group against folds assigned by row,
scoring every training row out of fold under both.

The first run used one fold seed, and capped losses came out 0.14% better under row folds.
One seed cannot separate a leak from fold-to-fold noise, so each scheme was repeated with
three seeds. The reading was set before those runs: a difference counts only if the two
schemes' ranges do not overlap.

| Target | Risk-group folds | Row folds | Relative | Rounds, group / row |
|---|---|---|---|---|
| reported claims, Poisson | 164,901 to 164,938 | 164,295 to 164,482 | -0.31% | 2,508 / 3,949 |
| priced claims, Poisson | 128,345 to 128,484 | 128,332 to 128,539 | 0.00% | 1,708 / 1,581 |
| capped losses, Tweedie | 19,762,432 to 19,774,348 | 19,744,417 to 19,750,727 | -0.10% | 206 / 203 |

On reported claims the leak is confirmed, and it shows in the stopping round as well as the
score. Row folds keep boosting 57% longer, because the validation fold keeps rewarding what
the training fold remembers. On priced claim counts there is nothing, as D3-2 found.

On capped losses, the benchmark's own target, row folds win at every seed. D3-2's "no leak
on priced claims" was a statement about counts, and amounts carry a small one. One possible
channel was checked and ruled out: copied amounts. In the 66 risk groups with priced
claims on two or more pieces, 16 pairs of claims on different pieces share an amount. Every
one of those amounts is 1,204, 1,172 or 1,128.12, among the settlement figures D2-6 found on
40.7% of claims, so that is what chance gives. The document offers a reading, that a Tweedie deviance on
capped rates weighs a claim on a short piece heavily, and labels it as a reading. The
risk-group split is used regardless.

## 2026-09-14 · A first look at the holdout, and nothing more

Scored once, after everything above was fixed:

| Model | Capped Tweedie deviance | Below constant | Capped A/E |
|---|---|---|---|
| constant | 5,284,486 | | |
| GLM | 5,008,494 | 5.22% | 1.005 |
| LightGBM | 4,964,221 | 6.06% | 1.006 |

LightGBM gets 16% further below the constant than the GLM. The two models' log predictions
correlate at 0.91 on the holdout. Bonus-malus carries 55.7% of the trees' split gain, and
region comes next at 11.9%. No claim is made about whether the gap is real. The deviance is
dominated by claims on short exposures, and D3-6 bootstraps it over risk groups.

---

## 2026-09-14 · Which Gini, written down before any was computed

The plan's pitfall for this commit is reporting a Gini without saying which one. Before any
Gini was computed, the reported one was fixed as the ordered Lorenz Gini on capped losses:

- **Ordering.** Held-out policy-years are sorted by predicted pure premium per policy-year,
  lowest first. It is the annual rate, not the expected loss for the row's exposure, because
  D3-1 fixed that as the unit of a price.
- **Axes.** The x-axis is cumulative exposure, so a policy-year counts by how long it ran. The
  y-axis is cumulative capped loss, because both models price large losses with the same flat
  load, so the capped losses are what they actually rate.
- **Score.** 1 minus twice the area under the curve, not normalized.
- **Ties.** Identical predictions are merged into one step, so the result does not depend on
  row order.

The recorded-loss version is shown beside it, and every other version is computed for
comparison and not reported. RESULTS.md puts the definition above the numbers, and a test
fails if it moves below them or goes missing.

| Model | Capped (reported) | Recorded |
|---|---|---|
| GLM | 0.319 | 0.291 |
| LightGBM | 0.338 | 0.269 |

## 2026-09-14 · On recorded losses the winner reverses, and ten claims are why

LightGBM ranks capped losses better. On recorded losses the GLM does. The ten largest held-out
policy losses are 21.8% of recorded losses, the largest 399,214. Without those ten policy-years
the recorded-loss Gini is 0.358 for LightGBM and 0.333 for the GLM, the capped order again.
Reported without the word "capped", either model could be called the better ranker from the
same predictions. D3-6 bootstraps both gaps.

The first draft said the recorded Gini "mostly reports which model happened to charge those
few policies more". That was an explanation, not a measurement, and the draft had nothing
behind it. The removal test was added to check it, and the sentence now states the result.

## 2026-09-14 · Six other things called Gini, and what each would have said

The same rows and predictions, plus two reference models. One is a constant premium. The other
is the GLM multiplied by independent mean-1 lognormal noise with σ = 1, the same prices with
their ranking deliberately damaged.

| Version | Constant | GLM | LightGBM | Noisy GLM |
|---|---|---|---|---|
| sorted highest premium first | 0.000 | -0.319 | -0.338 | -0.133 |
| ties broken by policy id | -0.068 | 0.319 | 0.338 | 0.133 |
| unweighted, normalized, by premium per year | 0.131 | 0.256 | 0.267 | 0.101 |
| unweighted, normalized, by expected loss | 0.168 | 0.327 | 0.339 | 0.214 |
| 2 × AUC − 1, any priced claim | 0.000 | 0.223 | 0.232 | 0.108 |
| Gini coefficient of the prices | 0.000 | 0.337 | 0.335 | 0.597 |

Three of these would have misled.

The unweighted version, the one common in machine-learning competitions, gives a constant
premium 0.131 from row order and 0.168 once rows are sorted by expected loss. The
second is credit for knowing how long a policy ran.

Row order is not neutral either. The lower half of held-out policy ids has a capped loss rate
of 134.8 per policy-year against 113.9, and a mean exposure of 0.578 against 0.481. Breaking
ties by id moves a constant model to -0.068 on the exposure-weighted curve, which reads the
lowest ids as the cheapest. The unweighted version reads them as the most expensive, and its
0.131 comes from the same order. Merging ties is what gives the reported definition exactly 0.

The Gini coefficient of the prices measures spread. It gives the noisy GLM its highest score,
0.597, while that model's reported Gini is the worst, 0.133.

## 2026-09-14 · The Lorenz figure is committed and regenerates byte for byte

`docs/figures/lorenz-curves.png` is drawn by the same script. The two series colours were run
through the palette validator first, and all checks pass on the light surface, with worst CVD
separation ΔE 24.7. The PNG is written without the matplotlib version in its metadata. The
document and the figure were each regenerated twice in separate processes and came out
identical.
