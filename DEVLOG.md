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
