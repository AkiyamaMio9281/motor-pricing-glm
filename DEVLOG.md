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
