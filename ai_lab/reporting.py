"""Portable reports and a human review sheet. No automatic accuracy claims."""
import csv
import html
import json
from pathlib import Path
from statistics import mean

from .core import LabError, read_json

SCORES = ["purpose_score", "io_score", "dependencies_score", "evidence_score", "uncertainty_score"]
REVIEW_FIELDS = ["record_id", "model", "sample", "repeat", *SCORES, "unsupported_claims", "notes"]
RESULT_FIELDS = ["record_id", "model", "sample", "repeat", "status", "json_valid", "schema_valid",
                 "evidence_checks_passed", "wall_ms", "load_ms", "prompt_tokens", "output_tokens",
                 "tokens_per_second", "ollama_allocated_mib", "size_vram_bytes", "cpu_verified", "error"]


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_safe(row.get(key, "")) for key in fields})


def md(value):
    return html.escape(str(value)).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("`", "&#96;")


def human_reviews(path, records):
    if not path.exists():
        return {}, []
    known = {r["record_id"] for r in records}
    scores, seen, warnings = {}, set(), []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(REVIEW_FIELDS).issubset(reader.fieldnames or []):
            raise LabError("review.csv is missing columns. Restore its original header.", "review_error")
        for row in reader:
            key = row["record_id"]
            if key not in known or key in seen:
                raise LabError(f"Unknown or duplicate review record_id: {key}", "review_error")
            seen.add(key)
            values = [(row.get(k) or "").strip() for k in SCORES]
            if any(v and v not in {"0", "1", "2"} for v in values):
                raise LabError(f"Scores must be blank, 0, 1 or 2: {key}", "review_error")
            claims = (row.get("unsupported_claims") or "").strip()
            if claims and not claims.isdigit():
                raise LabError(f"unsupported_claims must be a non-negative integer: {key}", "review_error")
            if all(values):
                scores[key] = sum(map(int, values))
            elif any(values):
                warnings.append(f"Partial review for {key}; not included in human score averages.")
    return scores, warnings


def regenerate(folder):
    folder = Path(folder)
    metadata = read_json(folder / "run.json")
    inputs = read_json(folder / "inputs.json")
    try:
        records = [json.loads(line) for line in (folder / "results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as error:
        raise LabError(f"Cannot read results.jsonl: {error}", "report_error") from error
    write_csv(folder / "results.csv", RESULT_FIELDS, records)
    review_path = folder / "review.csv"
    if not review_path.exists():
        write_csv(review_path, REVIEW_FIELDS, records)
    scores, warnings = human_reviews(review_path, records)
    demo = metadata["mode"] == "demo"
    lines = ["# Code Steward AI model comparison", "",
             "**DEMO ONLY: scripted responses. No real model, speed or memory benchmark.**" if demo else
             "Local Ollama evaluation. Automated checks do not measure semantic accuracy.", "",
             f"Run: `{md(metadata['run_id'])}` | State: **{md(metadata['status'])}** | Device requested: `{md(metadata['config']['device'])}`",
             f"Recorded rows: {len(records)} / {metadata['expected_records']}. Unrecorded work was not tested.", "",
             "| Model | Rows | Valid* | Invalid/errors/skips | Mean request ms | Mean human /10 (reviewed rows) |",
             "|---|---:|---:|---:|---:|---:|"]
    for model in metadata["models"]:
        rows = [r for r in records if r["model"] == model]
        valid = sum(r["status"] == "valid" for r in rows)
        times = [r["wall_ms"] for r in rows if r.get("wall_ms") is not None]
        graded = [scores[r["record_id"]] for r in rows if r["record_id"] in scores]
        latency = f"{mean(times):.1f}" if times else "N/A"
        human = f"{mean(graded):.2f} ({len(graded)})" if graded else "unreviewed"
        lines.append(f"| {md(model)} | {len(rows)} | {valid} | {len(rows)-valid} | {latency} | {human} |")
    lines += ["", "*Valid means complete response + JSON/schema checks + evidence line bounds + requested CPU verification.",
              "It does not establish whether the explanation or evidence is correct. Errors/skips remain in the denominator.",
              "Request timing includes model load when it occurs; see load_ms per row. Each model is unloaded before its batch.",
              "Memory is Ollama's post-response allocation snapshot, NOT peak process RAM, total application RAM or energy usage.",
              "A zero size_vram snapshot verifies no reported GPU allocation for that response; unavailable data is not treated as zero.",
              "", "## Human review", "",
              "Read the source snapshots below and each response. Fill the five 0/1/2 score columns in review.csv:",
              "0 = wrong/missing, 1 = partly correct, 2 = correct and sufficiently complete. Each column is required for a /10 total.",
              "For uncertainty, score whether missing information is handled honestly (not whether the model sounds confident).",
              "For evidence, check whether the cited code actually supports each claim. Count unsupported claims separately.",
              "Custom inputs have no reference rubric; the reviewer must supply the expected behavior.",
              "Run `python ai_test.py report <this-folder>` after saving the CSV. This never reruns a model or overwrites your scores.",
              "Do not compare averages from different sample sets/settings or mostly unreviewed rows.", ""]
    if metadata.get("errors") or warnings:
        lines += ["## Run notes", ""] + [f"- {md(e)}" for e in metadata.get("errors", []) + warnings] + [""]
    cases = {case["name"]: case for case in inputs}
    for row in records:
        lines += [f"## {md(row['record_id'])}: {md(row['model'])} / {md(row['sample'])}", "",
                  f"Status: **{md(row['status'])}**. Repeat: {row['repeat']}.", ""]
        case = cases[row["sample"]]
        if case.get("rubric"):
            lines += ["Reference checklist (not sent to the model):", ""]
            lines += [f"- **{md(k)}:** {md(v)}" for k, v in case["rubric"].items()]
            lines.append("")
        if row.get("error"):
            lines += [f"Check/error: {md(row['error'])}", ""]
        lines += ["Model response:", ""]
        # Indented blocks safely contain arbitrary backticks / Markdown in source/output.
        lines += ["    " + line for line in (row.get("raw_output") or "(no output)").splitlines()]
        lines += ["", "Source snapshot:", ""]
        lines += [f"    {n}: {line}" for n, line in enumerate(case.get("source", "(input skipped)").splitlines(), 1)]
        lines.append("")
    (folder / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return records
