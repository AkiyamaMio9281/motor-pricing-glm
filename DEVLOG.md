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
