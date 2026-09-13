"""Apply the versioned SQL migrations in migrations/ in filename order.

Why a runner at all, when `psql -f` in a loop would do: the mart is rebuilt
often during development, and a rebuild has to be safe to repeat. This gives
three properties that a loop does not.

  Applied once.   A migration already recorded in meta.schema_migrations is
                  skipped, so `migrate` is idempotent.
  Frozen once applied.  The sha256 of every applied file is recorded and
                  re-checked. Editing a migration that has already run against
                  a database is the single easiest way to make a mart that
                  nobody else can reproduce, so it is refused rather than
                  warned about. The fix is a new migration, or --reset.
  All or nothing. Each file runs inside one transaction. Postgres does
                  transactional DDL, so a syntax error halfway down a file
                  leaves no half-built schema behind.

Usage:
    python scripts/migrate.py              apply anything outstanding
    python scripts/migrate.py --status     list migrations and their state
    python scripts/migrate.py --reset      drop the project schemas, then apply
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

import psycopg

from db import REPO_ROOT, describe, dsn

MIGRATIONS_DIR = REPO_ROOT / "migrations"

# Dropped by --reset. meta is excluded on purpose: the bookkeeping table is
# truncated rather than dropped so that the schema itself never depends on a
# migration having created it.
PROJECT_SCHEMAS = ("raw", "stg", "dim", "fact", "mart", "model")

BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.schema_migrations (
    version     text        PRIMARY KEY,
    filename    text        NOT NULL,
    sha256      text        NOT NULL,
    duration_ms integer     NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_of(path: Path) -> str:
    """`001` from `001_raw.sql`. The numeric prefix is the identity."""
    prefix = path.name.split("_", 1)[0]
    if not prefix.isdigit():
        raise ValueError(
            f"{path.name} does not start with a numeric version prefix"
        )
    return prefix


def discover() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"no .sql files found in {MIGRATIONS_DIR}")

    seen: dict[str, Path] = {}
    for path in files:
        version = version_of(path)
        if version in seen:
            raise RuntimeError(
                f"duplicate migration version {version}: "
                f"{seen[version].name} and {path.name}"
            )
        seen[version] = path
    return files


def applied_state(conn: psycopg.Connection) -> dict[str, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, filename, sha256 FROM meta.schema_migrations")
        return {v: (f, s) for v, f, s in cur.fetchall()}


def reset(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for schema in PROJECT_SCHEMAS:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute("TRUNCATE meta.schema_migrations")
    conn.commit()
    print(f"  reset: dropped {', '.join(PROJECT_SCHEMAS)}")


def apply_one(conn: psycopg.Connection, path: Path) -> int:
    sql = path.read_text(encoding="utf-8")
    started = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(sql)
    duration_ms = int((time.perf_counter() - started) * 1000)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meta.schema_migrations "
            "(version, filename, sha256, duration_ms) VALUES (%s, %s, %s, %s)",
            (version_of(path), path.name, sha256_of(path), duration_ms),
        )
    conn.commit()
    return duration_ms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status", action="store_true", help="report state and exit"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop the project schemas before applying (destroys all data)",
    )
    args = parser.parse_args(argv)

    files = discover()

    with psycopg.connect(dsn()) as conn:
        print(f"connected to {describe()}")
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP)
        conn.commit()

        if args.reset:
            reset(conn)

        state = applied_state(conn)

        if args.status:
            for path in files:
                version = version_of(path)
                if version not in state:
                    mark = "pending"
                elif state[version][1] != sha256_of(path):
                    mark = "APPLIED, FILE CHANGED"
                else:
                    mark = "applied"
                print(f"  {version}  {path.name:<28} {mark}")
            return 0

        # Check every file before applying any, so a drifted migration is
        # caught before a later one has already been written to the database.
        drifted = [
            path.name
            for path in files
            if version_of(path) in state
            and state[version_of(path)][1] != sha256_of(path)
        ]
        if drifted:
            print(
                "refusing to migrate: these files were edited after being "
                "applied:\n  " + "\n  ".join(drifted),
                file=sys.stderr,
            )
            print(
                "add a new migration instead, or rebuild with --reset.",
                file=sys.stderr,
            )
            return 1

        pending = [p for p in files if version_of(p) not in state]
        if not pending:
            print("  nothing to apply")
            return 0

        for path in pending:
            duration_ms = apply_one(conn, path)
            print(f"  applied {path.name} in {duration_ms} ms")

    return 0


if __name__ == "__main__":
    sys.exit(main())
