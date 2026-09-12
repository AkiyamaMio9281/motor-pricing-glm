"""Load the freMTPL2 CSVs into the raw layer, and time how it was done.

Two methods, selectable so that the comparison in DEVLOG.md is reproducible
rather than a remembered number.

  executemany  Parse the CSV in Python, build tuples, hand them to psycopg in
               batches. This is the shape of every "just load it with pandas"
               approach: pandas.to_sql does the same thing one layer up, via
               SQLAlchemy. Measuring psycopg directly keeps the comparison about
               the loading mechanism instead of also changing the driver, and
               avoids adding SQLAlchemy as a dependency used for nothing else.
  copy         Stream the file's bytes straight into COPY FROM STDIN. Nothing is
               parsed in Python and there is no per-batch round trip.

The timing is wall clock for the whole operation, so the Python-side parsing
cost sits inside the executemany number and is absent from the copy number.
That is not a confound to correct for; it is most of the reason COPY wins, and
the point of the exercise is the end-to-end cost of getting a file into a table.

Every run appends to raw.load_audit, so the history of what was loaded, from
which file, by which method, and how long it took, is queryable later.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
import time
from pathlib import Path

import psycopg

from db import REPO_ROOT, describe, dsn

DATA_DIR = REPO_ROOT / "data"

TARGETS = {
    "freq": {
        "table": "raw.freq_raw",
        "csv": DATA_DIR / "freMTPL2freq.csv",
        "columns": [
            "idpol", "claimnb", "exposure", "area", "vehpower", "vehage",
            "drivage", "bonusmalus", "vehbrand", "vehgas", "density", "region",
        ],
    },
    "sev": {
        "table": "raw.sev_raw",
        "csv": DATA_DIR / "freMTPL2sev.csv",
        "columns": ["idpol", "claimamount"],
    },
}

BATCH = 10_000
CHUNK = 1 << 20


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def load_executemany(
    conn: psycopg.Connection, table: str, columns: list[str],
    csv_path: Path, limit: int | None,
) -> int:
    placeholders = ", ".join(["%s"] * len(columns))
    statement = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    )
    rows = 0
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader)  # header
        batch: list[tuple[str, ...]] = []
        with conn.cursor() as cur:
            for record in reader:
                batch.append(tuple(record))
                if len(batch) >= BATCH:
                    cur.executemany(statement, batch)
                    rows += len(batch)
                    batch.clear()
                    if limit and rows >= limit:
                        return rows
            if batch:
                cur.executemany(statement, batch)
                rows += len(batch)
    return rows


def load_copy(
    conn: psycopg.Connection, table: str, columns: list[str],
    csv_path: Path, limit: int | None,
) -> int:
    statement = (
        f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv)"
    )
    rows = 0
    with conn.cursor() as cur, cur.copy(statement) as copy:
        with csv_path.open("rb") as handle:
            handle.readline()  # header
            if limit:
                for line in handle:
                    copy.write(line)
                    rows += 1
                    if rows >= limit:
                        break
            else:
                # No decoding, no line splitting: shovel bytes.
                while block := handle.read(CHUNK):
                    copy.write(block)

    if not limit:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            rows = cur.fetchone()[0]
    return rows


METHODS = {"executemany": load_executemany, "copy": load_copy}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=sorted(TARGETS) + ["all"])
    parser.add_argument("--method", choices=sorted(METHODS), default="copy")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="load at most N rows, for quick checks; audited as such",
    )
    args = parser.parse_args(argv)

    names = sorted(TARGETS) if args.target == "all" else [args.target]

    with psycopg.connect(dsn()) as conn:
        print(f"connected to {describe()}")
        for name in names:
            spec = TARGETS[name]
            csv_path = spec["csv"]
            if not csv_path.exists():
                print(
                    f"{csv_path} is missing. Run scripts/fetch_data.py first.",
                    file=sys.stderr,
                )
                return 1

            checksum = sha256_of(csv_path)

            with conn.cursor() as cur:
                cur.execute(f"TRUNCATE {spec['table']}")
            conn.commit()

            started = time.perf_counter()
            rows = METHODS[args.method](
                conn, spec["table"], spec["columns"], csv_path, args.limit
            )
            conn.commit()
            duration_ms = int((time.perf_counter() - started) * 1000)

            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO raw.load_audit (table_name, src_file, "
                    "src_sha256, method, row_count, duration_ms) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        spec["table"], csv_path.name, checksum,
                        args.method if not args.limit
                        else f"{args.method} (limit {args.limit})",
                        rows, duration_ms,
                    ),
                )
            conn.commit()

            rate = rows / (duration_ms / 1000) if duration_ms else float("inf")
            print(
                f"  {spec['table']:<16} {args.method:<12} "
                f"{rows:>9,} rows  {duration_ms:>8,} ms  "
                f"{rate:>10,.0f} rows/s"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
