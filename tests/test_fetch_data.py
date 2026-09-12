"""Tests for the ARFF to CSV conversion.

The conversion is where a silent corruption would be easiest: ARFF quotes its
categoricals with single quotes, so a naive reader leaves 'B12' in the data and
every downstream join on vehicle brand quietly matches nothing. These tests pin
the unquoting, the column extraction, and the one failure that must be loud.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetch_data import DATASETS, arff_to_csv  # noqa: E402

SAMPLE = """@relation tiny
@attribute IDpol numeric
@attribute Exposure numeric
@attribute Area {'A','B'}
@attribute VehGas string
@data
1,0.1,'A','Regular'
2,0.77,'B','Diesel'
"""


def write_arff(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "tiny.arff"
    path.write_text(text, encoding="utf-8")
    return path


def test_columns_come_from_the_attribute_declarations(tmp_path):
    columns, rows = arff_to_csv(
        write_arff(tmp_path, SAMPLE), tmp_path / "out.csv"
    )
    assert columns == ["IDpol", "Exposure", "Area", "VehGas"]
    assert rows == 2


def test_single_quoted_categoricals_are_unquoted(tmp_path):
    out = tmp_path / "out.csv"
    arff_to_csv(write_arff(tmp_path, SAMPLE), out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "IDpol,Exposure,Area,VehGas"
    assert lines[1] == "1,0.1,A,Regular"
    assert "'" not in out.read_text(encoding="utf-8")


def test_missing_data_section_is_an_error(tmp_path):
    text = SAMPLE.split("@data")[0]
    with pytest.raises(RuntimeError, match="@data"):
        arff_to_csv(write_arff(tmp_path, text), tmp_path / "out.csv")


def test_no_attributes_is_an_error(tmp_path):
    text = "@relation tiny\n@data\n1,2\n"
    with pytest.raises(RuntimeError, match="@attribute"):
        arff_to_csv(write_arff(tmp_path, text), tmp_path / "out.csv")


def test_short_row_is_an_error_rather_than_a_shifted_row(tmp_path):
    """A row with fewer fields must stop the run.

    csv.writer would happily write the short row and every column after the
    gap would be shifted by one for that record only, which is the kind of
    defect that survives all the way to a rate table.
    """
    text = SAMPLE + "3,0.5,'A'\n"
    with pytest.raises(RuntimeError, match="expected 4"):
        arff_to_csv(write_arff(tmp_path, text), tmp_path / "out.csv")


def test_blank_trailing_lines_are_skipped(tmp_path):
    columns, rows = arff_to_csv(
        write_arff(tmp_path, SAMPLE + "\n\n"), tmp_path / "out.csv"
    )
    assert rows == 2


def test_pinned_checksums_are_full_length_md5():
    """A truncated or placeholder checksum would disable the upstream check."""
    for name, spec in DATASETS.items():
        assert len(spec["md5"]) == 32, name
        assert all(c in "0123456789abcdef" for c in spec["md5"]), name
        assert spec["expected_rows"] > 0, name
