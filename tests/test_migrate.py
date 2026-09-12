"""Tests for the migration runner's file-level invariants.

These are deliberately database-free. The properties worth pinning down here
are about how migration files are identified and ordered, and those are the
ones that go wrong quietly: a duplicate version prefix, or a file that sorts
differently from how a reader expects. The end-to-end apply path is exercised
by running migrate.py against the container.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import migrate  # noqa: E402
from db import load_env  # noqa: E402


def test_version_prefix_is_the_identity():
    assert migrate.version_of(Path("001_raw.sql")) == "001"
    assert migrate.version_of(Path("012_mart_experience.sql")) == "012"


def test_non_numeric_prefix_is_rejected():
    with pytest.raises(ValueError):
        migrate.version_of(Path("raw.sql"))


def test_discovered_versions_are_unique_and_sorted():
    files = migrate.discover()
    versions = [migrate.version_of(p) for p in files]
    assert len(versions) == len(set(versions)), "duplicate migration version"
    assert versions == sorted(versions), "filename order must be version order"


def test_zero_padding_keeps_lexical_and_numeric_order_together():
    """The prefix is zero-padded so that string sort equals numeric sort.

    Without padding, 10_x.sql sorts before 9_x.sql and the mart is built in the
    wrong order with no error. Every migration must stay three digits wide.
    """
    for path in migrate.discover():
        assert len(migrate.version_of(path)) == 3, (
            f"{path.name}: version prefix must be three digits"
        )


def test_sha256_changes_when_a_file_changes(tmp_path):
    target = tmp_path / "001_x.sql"
    target.write_text("SELECT 1;", encoding="utf-8")
    before = migrate.sha256_of(target)
    target.write_text("SELECT 1; -- edited", encoding="utf-8")
    assert migrate.sha256_of(target) != before


def test_missing_password_is_an_explicit_error(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    empty = tmp_path / ".env"
    empty.write_text("POSTGRES_DB=x\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        load_env(empty)


def test_environment_overrides_the_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_DB=from_file\nPOSTGRES_PASSWORD=p\n", encoding="utf-8"
    )
    monkeypatch.setenv("POSTGRES_DB", "from_environ")
    assert load_env(env_file)["POSTGRES_DB"] == "from_environ"
