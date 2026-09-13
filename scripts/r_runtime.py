"""Locate Rscript so Python code and tests can run the R side of the project.

R on Windows does not add itself to PATH by default, so `Rscript` is usually
not resolvable by name on the machine this project was built on. The search
order is the least surprising one:

  1. the RSCRIPT environment variable, for an explicit choice
  2. Rscript on PATH
  3. on Windows, the newest R under Program Files

Callers that need R and cannot find it should skip, not fail: a clone without R
installed can still run every test that does not cross the language boundary.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path


def find_rscript() -> Path | None:
    explicit = os.environ.get("RSCRIPT")
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None

    on_path = shutil.which("Rscript")
    if on_path:
        return Path(on_path)

    if sys.platform == "win32":
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "R"
        candidates = [
            p / "bin" / "Rscript.exe"
            for p in root.glob("R-*")
            if (p / "bin" / "Rscript.exe").is_file()
        ]

        def version(path: Path) -> tuple[int, ...]:
            match = re.search(r"R-(\d+)\.(\d+)\.(\d+)", str(path))
            return tuple(int(x) for x in match.groups()) if match else (0,)

        if candidates:
            return max(candidates, key=version)

    return None
