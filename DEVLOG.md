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
