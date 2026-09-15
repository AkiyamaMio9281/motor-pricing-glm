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
COMBO_ROLES = {"Category", "Series", "Y", "Y2", "Tooltips"}


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


def visuals() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in REPORT.glob("pages/*/visuals/*/visual.json")]


def test_the_folder_parameter_holds_no_local_path():
    expressions = (MODEL / "expressions.tmdl").read_text(encoding="utf-8")
    assert "expression ExtractFolder" in expressions
    assert "\\Users\\" not in expressions, "ExtractFolder holds a local path; restore the placeholder before committing"


def test_every_field_a_visual_uses_exists_in_the_model():
    fields = model_fields()
    all_visuals = visuals()
    assert len(all_visuals) >= 20
    missing = {ref for visual in all_visuals for ref in query_refs(visual, set())} - fields
    assert not missing, f"visuals refer to fields the model does not define: {sorted(missing)}"


def test_combo_charts_use_roles_power_bi_recognises():
    for visual in visuals():
        if visual["visual"]["visualType"] == "lineClusteredColumnComboChart":
            roles = set(visual["visual"]["query"]["queryState"])
            assert roles <= COMBO_ROLES, f"combo chart roles {sorted(roles - COMBO_ROLES)} render nothing"


def test_no_two_visuals_overlap_on_a_page():
    for page in REPORT.glob("pages/*/visuals"):
        boxes = []
        for path in page.glob("*/visual.json"):
            p = json.loads(path.read_text(encoding="utf-8"))["position"]
            boxes.append((path.parent.name, p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"]))
        overlaps = [(a[0], b[0]) for i, a in enumerate(boxes) for b in boxes[i + 1:]
                    if min(a[3], b[3]) > max(a[1], b[1]) and min(a[4], b[4]) > max(a[2], b[2])]
        assert not overlaps, f"overlapping visuals on page {page.parent.name}: {overlaps}"


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
        data = (REPO_ROOT / name).read_bytes()
        if bytes([0]) in data[:8000]:
            continue
        text = data.decode("utf-8", errors="ignore")
        found = {c for c in text if ord(c) > 127 and unicodedata.category(c).startswith("L")}
        if found:
            letters[name] = found
    assert not letters, f"non-English letters in the Power BI project: {letters}"
