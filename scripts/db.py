"""Connection settings for the L1 mart, read from .env.

Both the migration runner and the loaders import from here so that there is a
single place where the database address is decided. The R side (L2/L3) reads
the same .env rather than carrying its own copy of the settings.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

_DEFAULTS = {
    "POSTGRES_DB": "motor_pricing",
    "POSTGRES_USER": "pricing",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
}


def load_env(env_file: Path = ENV_FILE) -> dict[str, str]:
    """Return settings from .env, falling back to the real environment.

    Deliberately not python-dotenv: the file is five keys and adding a
    dependency to parse it would be the larger cost. Values already present in
    os.environ win, so CI can override without editing a file.
    """
    values = dict(_DEFAULTS)

    if env_file.exists():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

    for key in list(values) + ["POSTGRES_PASSWORD"]:
        if os.environ.get(key):
            values[key] = os.environ[key]

    if not values.get("POSTGRES_PASSWORD"):
        raise RuntimeError(
            f"POSTGRES_PASSWORD is not set. Copy .env.example to .env "
            f"(expected at {env_file}) or export it."
        )

    return values


def dsn(env_file: Path = ENV_FILE) -> str:
    """libpq connection string."""
    v = load_env(env_file)
    return (
        f"host={v['POSTGRES_HOST']} port={v['POSTGRES_PORT']} "
        f"dbname={v['POSTGRES_DB']} user={v['POSTGRES_USER']} "
        f"password={v['POSTGRES_PASSWORD']}"
    )


def describe(env_file: Path = ENV_FILE) -> str:
    """Same target as dsn(), with the password withheld, for log lines."""
    v = load_env(env_file)
    return (
        f"{v['POSTGRES_USER']}@{v['POSTGRES_HOST']}:{v['POSTGRES_PORT']}"
        f"/{v['POSTGRES_DB']}"
    )
