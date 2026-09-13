"""Fetch the freMTPL2 extracts from OpenML and convert them to CSV.

The data is not committed, so this script is the repository's only claim about
what the numbers in RESULTS.md were computed from. That makes the checksums the
important part, not the download.

Two things are checked, and they are not the same check:

  MD5 against a pinned constant.  EXPECTED_MD5 below is hardcoded, not read
      from the OpenML API. Comparing a download against a checksum fetched from
      the same API in the same run only proves the bytes arrived intact; it
      cannot notice that upstream replaced the file. A pinned value notices.
  SHA-256 of the converted CSVs.  Written to data/CHECKSUMS.txt so that a later
      question of the form "was the mart built from the same file I have" has an
      answer that does not depend on OpenML still being up.

OpenML serves ARFF. The header is 13 lines of @attribute declarations and the
body is CSV with single-quoted categoricals, so conversion is a header swap and
a requote rather than a parse.
"""

from __future__ import annotations

import csv
import hashlib
import io
import sys
import time
import urllib.request
from pathlib import Path

from db import REPO_ROOT

DATA_DIR = REPO_ROOT / "data"
CHECKSUM_FILE = DATA_DIR / "CHECKSUMS.txt"

# OpenML dataset ids 41214 and 41215. The MD5s are the values OpenML published
# for these files; see the module docstring for why they are pinned here.
DATASETS = {
    "freMTPL2freq": {
        "url": "https://openml.org/data/v1/download/20649148/freMTPL2freq.arff",
        "md5": "f8875568bf0ca622929105197e2db613",
        # The authoritative count is the one in the file. OpenML's own
        # description text for 41214 says 677,991, which disagrees with both
        # the file it serves and the CASdatasets original. See DEVLOG.
        "expected_rows": 678_013,
    },
    "freMTPL2sev": {
        "url": "https://openml.org/data/v1/download/20649149/freMTPL2sev.arff",
        "md5": "24cc74449e3931cb1aad0d43a12e7a6e",
        "expected_rows": 26_639,
    },
}

CHUNK = 1 << 20


def md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=180) as response:
        target.write_bytes(response.read())
    elapsed = time.perf_counter() - started
    mb = target.stat().st_size / 1e6
    print(f"    downloaded {mb:,.1f} MB in {elapsed:,.1f} s")


def arff_to_csv(arff_path: Path, csv_path: Path) -> tuple[list[str], int]:
    """Convert one ARFF to CSV, returning the column names and the row count.

    Read as one string rather than streamed: the larger file is 36 MB, well
    inside memory, and streaming would buy nothing but a more fragile reader.
    """
    text = arff_path.read_text(encoding="utf-8")

    header, separator, body = text.partition("@data")
    if not separator:
        raise RuntimeError(f"{arff_path.name}: no @data section")

    columns = [
        line.split()[1]
        for line in header.splitlines()
        if line.lower().startswith("@attribute")
    ]
    if not columns:
        raise RuntimeError(f"{arff_path.name}: no @attribute declarations")

    # ARFF quotes categoricals with single quotes; csv handles it as a dialect
    # rather than needing the values stripped by hand.
    reader = csv.reader(io.StringIO(body.lstrip("\r\n")), quotechar="'")

    rows = 0
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for record in reader:
            if not record:
                continue
            if len(record) != len(columns):
                raise RuntimeError(
                    f"{arff_path.name} line {rows + 1}: {len(record)} fields, "
                    f"expected {len(columns)}"
                )
            writer.writerow(record)
            rows += 1

    return columns, rows


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    checksums: list[str] = []

    for name, spec in DATASETS.items():
        print(f"{name}")
        arff_path = DATA_DIR / f"{name}.arff"
        csv_path = DATA_DIR / f"{name}.csv"

        if arff_path.exists() and md5_of(arff_path) == spec["md5"]:
            print("    arff present and matches pinned md5, not re-downloading")
        else:
            download(spec["url"], arff_path)
            actual = md5_of(arff_path)
            if actual != spec["md5"]:
                print(
                    f"    md5 mismatch\n"
                    f"      pinned {spec['md5']}\n"
                    f"      got    {actual}\n"
                    f"    upstream may have been replaced; do not build the "
                    f"mart from this file until the change is understood.",
                    file=sys.stderr,
                )
                return 1
            print("    md5 matches pinned value")

        columns, rows = arff_to_csv(arff_path, csv_path)
        if rows != spec["expected_rows"]:
            print(
                f"    row count {rows:,} does not match the expected "
                f"{spec['expected_rows']:,}",
                file=sys.stderr,
            )
            return 1
        print(f"    {rows:,} rows, {len(columns)} columns -> {csv_path.name}")

        checksums.append(f"{sha256_of(csv_path)}  {csv_path.name}")

    CHECKSUM_FILE.write_text(
        "# sha256 of the converted CSVs. Regenerate with scripts/fetch_data.py.\n"
        + "\n".join(checksums)
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {CHECKSUM_FILE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
