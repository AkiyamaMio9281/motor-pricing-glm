from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT = REPO_ROOT / "powerbi"
MODEL = PROJECT / "motor_pricing.SemanticModel" / "definition"
REPORT = PROJECT / "motor_pricing.Report" / "definition"


def model_fields() -> set[str]:
    fields = set()
    for path in (MODEL / "tables").glob("*.tmdl"):
        text = path.read_text(encoding="utf-8")
        table = re.search(r"^table (.+)$", text, re.M).group(1).strip().strip("'")
        for kind, name in re.findall(r"^\t(column|measure) ('[^']+'|[^\s=]+)", text, re.M):
            fields.add(f"{table}.{name.strip(chr(39))}")
    return fields


def query_refs(node, found):
    if isinstance(node, dict):
        if "queryRef" in node:
            found.add(node["queryRef"])
        for value in node.values():
            query_refs(value, found)
    elif isinstance(node, list):
        for value in node:
            query_refs(value, found)
    return found


def test_the_folder_parameter_holds_no_local_path():
    expressions = (MODEL / "expressions.tmdl").read_text(encoding="utf-8")
    assert "expression ExtractFolder" in expressions
    assert "\\Users\\" not in expressions, "ExtractFolder holds a local path; restore the placeholder before committing"


def test_every_field_a_visual_uses_exists_in_the_model():
    fields = model_fields()
    visuals = list(REPORT.glob("pages/*/visuals/*/visual.json"))
    assert len(visuals) >= 20
    missing = {ref for path in visuals for ref in query_refs(json.loads(path.read_text(encoding="utf-8")), set())} - fields
    assert not missing, f"visuals refer to fields the model does not define: {sorted(missing)}"


def test_machine_bound_files_are_ignored():
    for path in ("powerbi/motor_pricing.SemanticModel/.pbi/cache.abf",
                 "powerbi/motor_pricing.SemanticModel/.pbi/localSettings.json",
                 "powerbi/motor_pricing.Report/.pbi/localSettings.json"):
        ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=REPO_ROOT)
        assert ignored.returncode == 0, f"{path} is not ignored"


def test_the_project_text_is_english():
    tracked = subprocess.run(["git", "ls-files", "--others", "--cached", "--exclude-standard", "powerbi"],
                             cwd=REPO_ROOT, capture_output=True, text=True).stdout.split()
    letters = {}
    for name in tracked:
        text = (REPO_ROOT / name).read_bytes().decode("utf-8", errors="ignore")
        found = {c for c in text if ord(c) > 127 and unicodedata.category(c).startswith("L")}
        if found:
            letters[name] = found
    assert not letters, f"non-English letters in the Power BI project: {letters}"
