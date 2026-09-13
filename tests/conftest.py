"""Shared fixtures.

The staging tests need a loaded database. They skip rather than fail when one
is not reachable, so that `pytest` on a fresh clone reports the pure-Python
tests honestly instead of a wall of connection errors. A skip that says why is
more useful than a red run that everyone learns to ignore.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture(scope="session")
def conn():
    psycopg = pytest.importorskip("psycopg")
    from db import dsn

    try:
        connection = psycopg.connect(dsn(), connect_timeout=5)
    except Exception as exc:  # noqa: BLE001 - reported, not handled
        pytest.skip(f"no database reachable: {exc}")

    with connection:
        yield connection


@pytest.fixture(scope="session")
def staged(conn):
    """Require a staging layer built from loaded data.

    The distinction this fixture draws is the point of it. No data at all is a
    fresh clone and the staging tests have nothing to say, so they skip. Data in
    the raw layer but nothing in staging is a broken pipeline, and it must fail:
    an earlier version of this fixture skipped on an empty staging table, which
    is exactly the state a reset-before-load produces, and the suite reported
    green over a staging layer that had been built from nothing.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('stg.cleaning_audit')")
        if cur.fetchone()[0] is None:
            pytest.skip("stg layer not built; run scripts/migrate.py")

        cur.execute("SELECT count(*) FROM raw.freq_raw")
        if cur.fetchone()[0] == 0:
            pytest.skip("no data loaded; run scripts/load_raw.py")

        cur.execute("SELECT count(*) FROM stg.policy_cleaned")
        staged_rows = cur.fetchone()[0]

    assert staged_rows > 0, (
        "raw layer is loaded but stg.policy_cleaned is empty. The transform "
        "has not been run against the loaded data: scripts/transform.py"
    )
    return conn


def scalar(conn, sql: str):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def r_vector(path: Path, name: str) -> list[str]:
    """The elements of `NAME <- c(...)` in an R source file, as strings.

    Tests use this to compare constants the R side defines, such as the model's
    rating terms, with what a generated document says it was computed from.
    Quoted elements come back without their quotes; bare numbers as written.
    """
    import re

    source = path.read_text(encoding="utf-8")
    block = re.search(rf"^{re.escape(name)}\s*<-\s*c\((.*?)\)\s*$", source, re.S | re.M)
    assert block, f"could not find {name} <- c(...) in {path.name}"
    body = re.sub(r"#.*", "", block.group(1))
    quoted = re.findall(r'"([^"]+)"', body)
    return quoted if quoted else [t.strip() for t in body.split(",") if t.strip()]
