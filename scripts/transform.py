"""Run the re-runnable transforms in sql/transform/ in filename order.

Migrations and transforms are different things and this project keeps them
apart. A migration changes structure, runs once, and is frozen afterwards. A
transform reads one layer and rewrites the next, is safe against an empty
database only in the sense that it refuses to run, and is expected to be re-run
whenever the layer below it changes.

Collapsing the two is how the staging layer came to be built from an empty raw
layer: `migrate --reset` applied a migration that read from `raw.freq_raw`
before anything had been loaded into it. The result was not an error. It was a
complete, internally consistent audit trail describing nothing, and a test suite
that skipped instead of failing. See DEVLOG.

Hence the precondition below. A transform that would read nothing is a bug in
the pipeline order, not a no-op, and it exits non-zero.

Usage:
    python scripts/transform.py            run everything outstanding
    python scripts/transform.py --only 002 run one transform
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import psycopg

from db import REPO_ROOT, describe, dsn

TRANSFORM_DIR = REPO_ROOT / "sql" / "transform"

# Nothing downstream of the raw layer can be built before it is loaded. Checked
# once, up front, rather than left to each transform to notice.
REQUIRED_SOURCES = ("raw.freq_raw", "raw.sev_raw")


def version_of(path: Path) -> str:
    prefix = path.name.split("_", 1)[0]
    if not prefix.isdigit():
        raise ValueError(f"{path.name} does not start with a version prefix")
    return prefix


def discover(only: str | None) -> list[Path]:
    files = sorted(TRANSFORM_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"no .sql files found in {TRANSFORM_DIR}")
    if only:
        files = [p for p in files if version_of(p) == only]
        if not files:
            raise RuntimeError(f"no transform with version {only}")
    return files


def check_sources(conn: psycopg.Connection) -> list[str]:
    """Return the sources that are missing or empty."""
    problems = []
    for table in REQUIRED_SOURCES:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (table,))
            if cur.fetchone()[0] is None:
                problems.append(f"{table} does not exist")
                continue
            cur.execute(f"SELECT count(*) FROM {table}")
            if cur.fetchone()[0] == 0:
                problems.append(f"{table} is empty")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="run just this version prefix, e.g. 002")
    args = parser.parse_args(argv)

    files = discover(args.only)

    with psycopg.connect(dsn()) as conn:
        print(f"connected to {describe()}")

        problems = check_sources(conn)
        if problems:
            print(
                "refusing to transform:\n  " + "\n  ".join(problems) + "\n"
                "run scripts/migrate.py, then scripts/fetch_data.py and "
                "scripts/load_raw.py, then transform.",
                file=sys.stderr,
            )
            return 1

        for path in files:
            sql = path.read_text(encoding="utf-8")
            started = time.perf_counter()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
            except psycopg.Error as exc:
                # The file ran in one transaction, so rolling back leaves every
                # table this transform touches exactly as it was before. Say so,
                # because the first question after a failed rebuild is whether
                # the mart is now half-built.
                conn.rollback()
                diag = exc.diag
                print(
                    f"  FAILED {path.name}: {diag.message_primary or exc}",
                    file=sys.stderr,
                )
                if diag.message_detail:
                    print(f"    {diag.message_detail}", file=sys.stderr)
                print(
                    f"    rolled back; tables written by {path.name} are "
                    f"unchanged, and later transforms were not run.",
                    file=sys.stderr,
                )
                return 1
            conn.commit()
            duration_ms = int((time.perf_counter() - started) * 1000)
            print(f"  ran {path.name} in {duration_ms:,} ms")

    return 0


if __name__ == "__main__":
    sys.exit(main())
