"""Versioned, labeled test suites. References stay outside the model prompt."""
from datetime import datetime, timezone
from pathlib import Path
import uuid

from .core import LAB, LabError, collect_cases, digest, read_json, snapshot
from .reporting import save_json, write_csv

DATASETS = LAB / "datasets" / "v1"
MODEL_SUITES = ("smoke", "dev", "holdout")
RUBRIC_FIELDS = {"purpose", "inputs_outputs", "dependencies", "evidence", "unknowns", "must_not_claim"}


def load_suite(name, config):
    if name == "smoke":
        cases = collect_cases(None, config)
        for case in cases:
            case.update(suite="smoke", category="starter", difficulty="starter")
        return cases
    if name not in {"dev", "holdout", "robustness"}:
        raise LabError(f"Unknown test suite: {name}", "dataset_error")
    root = DATASETS / name
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("suite") != name or not isinstance(manifest.get("version"), str):
        raise LabError(f"Invalid suite manifest: {manifest_path}", "dataset_error")
    entries = manifest.get("cases")
    if not isinstance(entries, list) or not entries or len(entries) > config["max_files"]:
        raise LabError("Suite must contain 1..max_files cases.", "dataset_error")
    cases, identifiers, filenames = [], set(), set()
    for item in entries:
        if not isinstance(item, dict):
            raise LabError("Each manifest case must be an object.", "dataset_error")
        fields = ("id", "file", "category", "family", "difficulty")
        if any(not isinstance(item.get(k), str) or not item[k].strip() for k in fields):
            raise LabError("Case ID, file, category, family and difficulty are required.", "dataset_error")
        relative = Path(item["file"])
        path = root / relative
        if relative.is_absolute() or ".." in relative.parts or not path.resolve().is_relative_to(root.resolve()):
            raise LabError("Case path must stay inside its suite.", "dataset_error")
        for candidate in [path, *path.parents]:
            if candidate == root.parent:
                break
            if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
                raise LabError("Suite inputs must be real files, not links.", "dataset_error")
        if not path.is_file():
            raise LabError(f"Missing case file: {path}", "dataset_error")
        canonical = str(path.resolve()).casefold()
        if item["id"] in identifiers or canonical in filenames:
            raise LabError("Duplicate case ID or input file in suite.", "dataset_error")
        identifiers.add(item["id"])
        filenames.add(canonical)
        rubric = item.get("rubric")
        if not isinstance(rubric, dict) or not RUBRIC_FIELDS.issubset(rubric) or not all(isinstance(v, str) and v for v in rubric.values()):
            raise LabError(f"Incomplete reference rubric: {item['id']}", "dataset_error")
        expected = item.get("expected_input_error")
        if (name == "robustness" and expected not in {"input_empty", "input_encoding", "input_binary", "input_too_large", "input_extension"}) or (name != "robustness" and expected is not None):
            raise LabError("Invalid expected input outcome.", "dataset_error")
        case = snapshot(path, relative.as_posix(), config["max_source_chars"], rubric)
        case.update(case_id=item["id"], suite=name, dataset_version=manifest["version"],
                    manifest_hash=digest(manifest_path.read_bytes()), category=item["category"],
                    family=item["family"], difficulty=item["difficulty"],
                    expected_input_error=expected, evidence_lines=item.get("evidence_lines", []))
        evidence = case["evidence_lines"]
        if not isinstance(evidence, list) or any(type(n) is not int or n < 1 for n in evidence):
            raise LabError("Reference evidence must be positive line numbers.", "dataset_error")
        if "source" in case and any(n > case["facts"]["line_count"] for n in evidence):
            raise LabError(f"Reference evidence is outside {item['file']}.", "dataset_error")
        cases.append(case)
    return cases


def check_inputs(config, output_root):
    """Offline framework checks, with no model preparation or inference at all."""
    cases = load_suite("robustness", config)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    folder = output_root / ("input-checks-" + run_id)
    folder.mkdir(parents=True, exist_ok=False)
    rows = []
    for case in cases:
        actual = case.get("input_error_code", "accepted")
        rows.append({"case_id": case["case_id"], "file": case["name"],
                     "expected": case["expected_input_error"], "actual": actual,
                     "passed": actual == case["expected_input_error"], "message": case.get("input_error", "")})
    save_json(folder / "input_checks.json", {"mode": "input_validation_only", "dataset_version": cases[0]["dataset_version"],
                                             "manifest_hash": cases[0]["manifest_hash"], "config": config, "cases": rows})
    write_csv(folder / "input_checks.csv", ["case_id", "file", "expected", "actual", "passed", "message"], rows)
    passed = sum(row["passed"] for row in rows)
    lines = ["# Input validation checks", "", "No AI model was loaded or called. These are framework checks, not model scores.", "",
             f"Passed: {passed}/{len(rows)}", "", "| Case | Expected | Actual | Passed |", "|---|---|---|---|"]
    lines += [f"| {r['case_id']} | {r['expected']} | {r['actual']} | {r['passed']} |" for r in rows]
    (folder / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Input checks: {passed}/{len(rows)} passed. No Ollama needed.\nResults: {folder}")
    return 0 if passed == len(rows) else 1
