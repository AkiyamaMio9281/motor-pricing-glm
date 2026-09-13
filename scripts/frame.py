"""The frequency model frame for the Python side, read from model.frequency_frame.

This is the Python twin of R/model_frame.R. Neither does any joins: the view is
the single definition of a policy-year as a model sees it, and
tests/test_model_frame.py checks that both languages receive byte-identical
frames by comparing an md5 over a canonical rendering of every row.

Two Python-specific choices, both measured:

  Rows come through a cursor, not pandas.read_sql. read_sql on a raw DBAPI
  connection is unsupported in pandas and warns, and the alternative of
  COPY-to-CSV-then-read_csv would put pandas' own float parser between the
  database and the frame, which is one more thing that has to round-trip
  exactly for the cross-language check to mean anything. psycopg parses float8
  with Python's float(), which is correctly rounded.

  exposure arrives as float because the view casts it. Read from the fact table
  it would be decimal.Decimal, and a pandas column of Decimals is object dtype:
  slow, and not something numpy will do arithmetic on without a conversion.
"""

from __future__ import annotations

import hashlib
import re

import pandas as pd
import psycopg

CATEGORICAL_COLUMNS = ("area", "veh_brand", "veh_gas", "region")
FLAG_COLUMNS = ("is_short_exposure", "is_high_claim_count", "is_exposure_capped")

# Column order of the canonical rendering. Must match R/check_frame.R exactly.
CANONICAL_ORDER = (
    "idpol", "claim_nb", "exposure", "area", "veh_power", "veh_age",
    "driv_age", "bonus_malus", "veh_brand", "veh_gas", "density", "region",
    "is_short_exposure", "is_high_claim_count", "is_exposure_capped",
)


def _natural(codes: list[str]) -> list[str]:
    """B2 before B10, matching the level order R/model_frame.R uses."""
    return sorted(codes, key=lambda c: (int(re.sub(r"\D", "", c) or 0), c))


def dimension_levels(conn: psycopg.Connection) -> dict[str, list[str]]:
    def column(sql: str) -> list[str]:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [row[0] for row in cur.fetchall()]

    return {
        "area": _natural(column("SELECT area_code FROM dim.area")),
        "veh_brand": _natural(column("SELECT DISTINCT veh_brand FROM dim.vehicle")),
        "veh_gas": sorted(column("SELECT DISTINCT veh_gas FROM dim.vehicle")),
        "region": _natural(column("SELECT region_code FROM dim.region")),
    }


def load_frequency_frame(conn: psycopg.Connection) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM model.frequency_frame ORDER BY idpol")
        columns = [d.name for d in cur.description]
        frame = pd.DataFrame(cur.fetchall(), columns=columns)
        cur.execute("SELECT count(*) FROM fact.exposure")
        expected_rows = cur.fetchone()[0]

    if len(frame) != expected_rows:
        raise ValueError(
            f"frame has {len(frame)} rows, fact.exposure has {expected_rows}"
        )
    if frame.isna().any().any():
        raise ValueError(
            "null values in: " + ", ".join(frame.columns[frame.isna().any()])
        )
    if any(c.endswith("_key") for c in frame.columns):
        raise ValueError(
            "surrogate keys in the frame would be fitted as continuous covariates"
        )
    if frame["exposure"].dtype != "float64":
        raise ValueError(
            f"exposure is {frame['exposure'].dtype}, expected float64; "
            "a numeric column read without the view's cast arrives as Decimal"
        )
    if not ((frame["exposure"] > 0) & (frame["exposure"] <= 1)).all():
        raise ValueError("exposure must be in (0, 1]")
    if (frame["claim_nb"] < 0).any():
        raise ValueError("claim_nb must be non-negative")

    levels = dimension_levels(conn)
    for column in CATEGORICAL_COLUMNS:
        unknown = set(frame[column]) - set(levels[column])
        if unknown:
            raise ValueError(f"{column} has values not in its dimension: {unknown}")
        frame[column] = pd.Categorical(frame[column], categories=levels[column])

    return frame


def canonical_md5(frame: pd.DataFrame) -> str:
    """md5 over one line per row, rendered exactly as R/check_frame.R renders it.

    exposure uses %.17g, enough significant digits to identify a double
    uniquely, so both languages print identical text for identical bits. Flags
    are 0 and 1. Lines end in a bare newline.
    """
    parts = []
    for column in CANONICAL_ORDER:
        series = frame[column]
        if column == "exposure":
            rendered = series.map(lambda x: "%.17g" % x)
        elif column in FLAG_COLUMNS:
            rendered = series.astype(int).astype(str)
        else:
            rendered = series.astype(str)
        parts.append(rendered.reset_index(drop=True))

    lines = parts[0]
    for part in parts[1:]:
        lines = lines + "|" + part

    digest = hashlib.md5()
    digest.update(("\n".join(lines) + "\n").encode("utf-8"))
    return digest.hexdigest()


def fingerprint(frame: pd.DataFrame) -> dict:
    return {
        "rows": len(frame),
        "claims": int(frame["claim_nb"].sum()),
        "exposure_sum": round(float(frame["exposure"].sum()), 6),
        "short_exposure_rows": int(frame["is_short_exposure"].sum()),
        "levels": {c: len(frame[c].cat.categories) for c in CATEGORICAL_COLUMNS},
        "md5": canonical_md5(frame),
    }


if __name__ == "__main__":
    import json
    import sys
    import time

    from db import dsn

    # Timed around connect and load only, the same span R/check_frame.R times,
    # so the two load_seconds values are comparable. Hashing is excluded.
    started = time.perf_counter()
    with psycopg.connect(dsn()) as conn:
        loaded = load_frequency_frame(conn)
    load_seconds = round(time.perf_counter() - started, 2)
    result = fingerprint(loaded)
    result["load_seconds"] = load_seconds
    print(json.dumps(result))
