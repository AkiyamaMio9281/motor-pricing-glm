"""Regenerate docs/explain-plans.md from the queries in sql/explain/.

The execution plans in the documentation are only worth including if someone
else can produce them again. So this script does not record plans that were
captured once by hand; it measures from the repository's final state every time.

The trick that makes that possible is that DROP INDEX is transactional in
Postgres. The "without index" measurement drops the index inside a transaction,
runs EXPLAIN ANALYZE, and rolls back. The index is back before the next run, and
nothing about the database has to be rebuilt to show what it was like before.
Every run, including the ones that DELETE, happens in its own rolled-back
transaction, so each starts from the same data.

Each query is run RUNS times per variant and the median execution time is
reported. The plan shown is from the final run.

The load comparison is measured differently, by wall clock, because what it
compares is a sequence of statements rather than one: an INSERT with per-row
foreign-key triggers against suspend, INSERT, restore.

Usage:
    python scripts/explain_plans.py            regenerate the document
    python scripts/explain_plans.py --runs 9   more runs per variant
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

from db import REPO_ROOT, describe, dsn

EXPLAIN_DIR = REPO_ROOT / "sql" / "explain"
TRANSFORM_004 = REPO_ROOT / "sql" / "transform" / "004_dim_fact.sql"
OUTPUT = REPO_ROOT / "docs" / "explain-plans.md"

EXEC_TIME = re.compile(r"Execution Time: ([0-9.]+) ms")


@dataclass
class Study:
    path: Path
    title: str
    index: str | None
    why: str
    sql: str


@dataclass
class Result:
    median_ms: float
    runs_ms: list[float]
    plan: str


def parse_study(path: Path) -> Study:
    header: dict[str, str] = {}
    body: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"--\s*(title|index|why):\s*(.*)", line)
        if match:
            header[match.group(1)] = match.group(2).strip()
        elif not line.startswith("--"):
            body.append(line)

    for key in ("title", "index", "why"):
        if key not in header:
            raise ValueError(f"{path.name}: missing '-- {key}:' header")

    return Study(
        path=path,
        title=header["title"],
        index=None if header["index"] == "none" else header["index"],
        why=header["why"],
        sql="\n".join(body).strip(),
    )


def resolve_placeholders(conn: psycopg.Connection, sql: str) -> str:
    """Replace {region:R43} with its region_key.

    The query needs a literal so the planner sees the real selectivity; a
    subquery or a join to dim.region would hide it behind a generic estimate and
    change the plan being documented. The key comes from this database, so the
    substitution is an integer we just read, not user input.
    """
    def lookup(match: re.Match) -> str:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT region_key FROM dim.region WHERE region_code = %s",
                (match.group(1),),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown region code {match.group(1)}")
        return str(int(row[0]))

    return re.sub(r"\{region:([A-Z0-9]+)\}", lookup, sql)


def explain(
    conn: psycopg.Connection, sql: str, drop_index: str | None, runs: int
) -> Result:
    times: list[float] = []
    plan = ""
    for _ in range(runs):
        with conn.cursor() as cur:
            if drop_index:
                cur.execute(f"DROP INDEX {drop_index}")
            cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) {sql}")
            lines = [row[0] for row in cur.fetchall()]
        conn.rollback()

        plan = "\n".join(lines)
        match = EXEC_TIME.search(plan)
        if not match:
            raise RuntimeError(f"no execution time in plan:\n{plan}")
        times.append(float(match.group(1)))

    return Result(statistics.median(times), times, plan)


def load_statement() -> str:
    """The fact.exposure INSERT, taken from transform 004 rather than copied.

    A copy would drift from the transform it claims to measure.
    """
    text = TRANSFORM_004.read_text(encoding="utf-8")
    match = re.search(r"(INSERT INTO fact\.exposure\s+SELECT.*?;)", text, re.S)
    if not match:
        raise RuntimeError("could not find the fact.exposure INSERT in 004")
    return match.group(1)


def time_load(conn: psycopg.Connection, suspend: bool, runs: int) -> list[float]:
    insert = load_statement()
    times = []
    for _ in range(runs):
        with conn.cursor() as cur:
            cur.execute("TRUNCATE fact.claim, fact.exposure")
            started = time.perf_counter()
            if suspend:
                cur.execute("SELECT meta.suspend_foreign_keys('fact.exposure')")
            cur.execute(insert)
            if suspend:
                cur.execute("SELECT meta.restore_foreign_keys('fact.exposure')")
            times.append((time.perf_counter() - started) * 1000)
        conn.rollback()
    return times


def trigger_profile(conn: psycopg.Connection) -> str:
    """EXPLAIN ANALYZE on the load, which reports time per foreign-key trigger."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE fact.claim, fact.exposure")
        cur.execute(f"EXPLAIN (ANALYZE, COSTS OFF) {load_statement()}")
        lines = [row[0] for row in cur.fetchall()]
    conn.rollback()
    return "\n".join(line for line in lines if line.startswith(("Trigger", "Execution", "Planning")))


def render(
    studies: list[tuple[Study, Result, Result | None]],
    load_fk: list[float],
    load_suspended: list[float],
    triggers: str,
    runs: int,
) -> str:
    out: list[str] = []
    out.append("# Execution plans\n")
    out.append(
        "Generated by `scripts/explain_plans.py` against the loaded mart. "
        "Do not edit by hand; re-run the script.\n"
    )
    out.append(
        f"Each variant ran {runs} times in its own rolled-back transaction; "
        "times are medians. The *without index* variant drops the index inside "
        "the transaction, which Postgres allows because `DROP INDEX` is "
        "transactional, so both variants are measured from the same final "
        "schema. Postgres 16, stock settings, Docker on Windows 11.\n"
    )

    out.append("## Summary\n")
    out.append("| Query | Index | Without | With | Change |")
    out.append("|---|---|---|---|---|")
    for study, with_idx, without_idx in studies:
        if without_idx is None:
            out.append(
                f"| {study.title} | none | {with_idx.median_ms:,.2f} ms | | |"
            )
            continue
        ratio = without_idx.median_ms / with_idx.median_ms
        change = f"{ratio:,.1f}x faster" if ratio >= 1.15 else "no gain"
        out.append(
            f"| {study.title} | `{study.index}` | "
            f"{without_idx.median_ms:,.2f} ms | {with_idx.median_ms:,.2f} ms | "
            f"{change} |"
        )
    out.append("")

    fk_med = statistics.median(load_fk)
    sus_med = statistics.median(load_suspended)
    out.append("## Loading fact.exposure\n")
    out.append(
        "678,013 rows, wall clock for the whole sequence, "
        f"{runs} runs each.\n"
    )
    out.append("| Method | Median |")
    out.append("|---|---|")
    out.append(f"| INSERT with five per-row foreign-key triggers | {fk_med:,.0f} ms |")
    out.append(
        f"| suspend foreign keys, INSERT, restore set-based | {sus_med:,.0f} ms |"
    )
    out.append(f"\n{fk_med / sus_med:,.1f}x. Where the time went, per trigger:\n")
    out.append("```\n" + triggers + "\n```\n")

    for study, with_idx, without_idx in studies:
        out.append(f"## {study.title}\n")
        out.append(f"{study.why[0].upper() + study.why[1:]}.\n")
        out.append("```sql\n" + study.sql + "\n```\n")
        if without_idx is not None:
            out.append(f"### Without `{study.index}`\n")
            out.append(
                "Runs: " + ", ".join(f"{t:,.2f}" for t in without_idx.runs_ms)
                + " ms\n"
            )
            out.append("```\n" + without_idx.plan + "\n```\n")
            out.append(f"### With `{study.index}`\n")
        out.append(
            "Runs: " + ", ".join(f"{t:,.2f}" for t in with_idx.runs_ms) + " ms\n"
        )
        out.append("```\n" + with_idx.plan + "\n```\n")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(argv)

    files = sorted(EXPLAIN_DIR.glob("*.sql"))
    if not files:
        print(f"no queries in {EXPLAIN_DIR}", file=sys.stderr)
        return 1

    with psycopg.connect(dsn(), autocommit=False) as conn:
        print(f"connected to {describe()}")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM fact.exposure")
            if cur.fetchone()[0] == 0:
                print("fact.exposure is empty; run the pipeline first", file=sys.stderr)
                return 1
        conn.rollback()

        # VACUUM cannot run inside a transaction, and index-only scans depend on
        # the visibility map it maintains, so the baseline for every plan below
        # is a freshly vacuumed and analysed table.
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("VACUUM ANALYZE fact.exposure")
            cur.execute("VACUUM ANALYZE fact.claim")
        conn.autocommit = False

        studies = []
        for path in files:
            study = parse_study(path)
            study.sql = resolve_placeholders(conn, study.sql)
            conn.rollback()
            print(f"  {path.name}")
            with_idx = explain(conn, study.sql, None, args.runs)
            without_idx = (
                explain(conn, study.sql, study.index, args.runs)
                if study.index else None
            )
            studies.append((study, with_idx, without_idx))

        print("  load comparison")
        load_fk = time_load(conn, suspend=False, runs=args.runs)
        load_suspended = time_load(conn, suspend=True, runs=args.runs)
        triggers = trigger_profile(conn)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        render(studies, load_fk, load_suspended, triggers, args.runs),
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
