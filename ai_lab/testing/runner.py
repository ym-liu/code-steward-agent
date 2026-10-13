"""Sequential experiments with source snapshots and persisted partial results."""
from datetime import datetime, timezone
import json
import os
import platform
import re
import subprocess
import time
import uuid

from ..common import ROOT, LabError, digest, save_json
from ..explain.answer import SCHEMA, SCHEMA_VERSION, validate_analysis
from ..explain.checks import (CHECKS_VERSION, LIST_FIELDS, check_claims, clean_answer, fill_from_facts,
                              needs_repair, repair_message)
from ..explain.request import INPUT_VERSION, PROMPT, source_view
from .live_status import LiveStatus, clock_text
from .report_page import friendly_error
from .report_records import regenerate


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def git_revision():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def performance_cores():
    """Physical performance cores on Apple Silicon/Intel Macs, which Ollama uses for num_thread "auto"."""
    for key in ("hw.perflevel0.physicalcpu", "hw.physicalcpu"):
        try:
            return int(subprocess.check_output(["sysctl", "-n", key], text=True,
                                               stderr=subprocess.DEVNULL, timeout=3).strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


class DemoClient:
    """Pipeline demonstration only. Never used as fallback for a real model."""
    def version(self):
        return "scripted-demo"

    def prepare(self, tag, allow_pull=False):
        return {"name": tag, "digest": None, "details": {"demo": True}}

    def analyze(self, tag, case):
        return {"done": True, "done_reason": "stop", "message": {"content": json.dumps({
            "purpose": None, "inputs": [], "outputs": [], "usage": [], "dependencies": [], "relationships": [],
            "unknowns": ["Demo only: no real model was used."],
            "evidence": [],
        })}}

    def running(self):
        return []

    def unload(self, tag):
        pass


def numeric(response, key):
    value = response.get(key)
    return value if type(value) in (int, float) and value >= 0 else None


def dropped_parts(first, second, issues):
    """Fields where the corrected answer lost items that had no problems."""
    flagged = {(issue["field"], issue["item"]) for issue in issues}
    lost = [field for field in LIST_FIELDS
            if len(second[field]) < sum((field, item) not in flagged for item in first[field])]
    if first["purpose"] is not None and second["purpose"] is None and ("purpose", first["purpose"]) not in flagged:
        lost.append("purpose")
    return lost


def repair_once(client, tag, case, raw, first, issues, progress=None):
    """Ask once for a corrected answer; keep it only if it is valid, has fewer problems and loses nothing else."""
    started = time.perf_counter()
    feedback = repair_message(issues)
    try:
        response = (client.repair(tag, case, raw, feedback, on_chunk=progress.on_chunk) if progress
                    else client.repair(tag, case, raw, feedback))
    except LabError as error:
        if error.code in {"timeout", "connection_error", "response_error"}:
            raise  # transport failures still stop the run
        return {"requested_for": issues, "used": False, "error": str(error)}
    message = response.get("message") if isinstance(response.get("message"), dict) else {}
    content = message.get("content")
    result = {"requested_for": issues, "used": False,
              "wall_ms": round((time.perf_counter() - started) * 1000, 2),
              "prompt_tokens": numeric(response, "prompt_eval_count"),
              "output_tokens": numeric(response, "eval_count"),
              **{key: round(numeric(response, original) / 1e6, 2) for original, key in
                 (("prompt_eval_duration", "read_ms"), ("eval_duration", "write_ms"))
                 if numeric(response, original) is not None},
              "done_reason": response.get("done_reason"), "raw_output": content}
    if not isinstance(content, str):
        result["error"] = "The repair response had no text message."
        return result
    analysis, _, schema_valid, errors = validate_analysis(content, case["facts"]["line_count"])
    if response.get("done") is not True or response.get("done_reason") not in {None, "stop"}:
        errors.append("Repair generation was incomplete or stopped at a limit.")
    if message.get("tool_calls"):
        errors.append("Unexpected tool call in the repair answer.")
    if not schema_valid or errors:
        result["error"] = "; ".join(errors) or "The repair answer failed format checks."
        return result
    cleaned = clean_answer(analysis)
    if cleaned:
        result["cleaned"] = cleaned
    filled = fill_from_facts(analysis, case)
    if filled:
        result["filled"] = filled
    lost = dropped_parts(first, analysis, issues)
    if lost:
        result["error"] = "The corrected answer dropped parts that had no problems: " + ", ".join(lost) + "."
        return result
    after = check_claims(analysis, case)
    emptied = {i["field"] for i in after if i["item"] is None} - {i["field"] for i in issues if i["item"] is None}
    if emptied:
        result["error"] = "The corrected answer left these parts empty: " + ", ".join(sorted(emptied)) + "."
        return result
    score = lambda found: (sum(not issue.get("minor") for issue in found), len(found))
    result.update(issues_after=after, used=score(after) < score(issues), analysis=analysis)
    return result


def result_for(client, tag, case, config, demo=False, live=False):
    row = {"status": "input_skipped", "json_valid": False, "schema_valid": False,
           "evidence_checks_passed": False, "wall_ms": None, "cpu_verified": None}
    if "input_error" in case:
        row["error"] = case["input_error"]
        row["error_code"] = case.get("input_error_code")
        return row
    shown, left_out = source_view(case, config)[1:]
    if left_out:
        row["source_view"] = {"shown_lines": len(shown), "left_out_lines": left_out}
    start = time.perf_counter()
    progress = LiveStatus() if live and not demo else None
    try:
        if progress:
            progress.start()
        response = client.analyze(tag, case, on_chunk=progress.on_chunk) if progress else client.analyze(tag, case)
        row["wall_ms"] = None if demo else round((time.perf_counter() - start) * 1000, 2)
        message = response.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise LabError("Ollama returned no text message.", "response_error")
        raw = message["content"]
        analysis, json_valid, schema_valid, errors = validate_analysis(raw, case["facts"]["line_count"])
        row.update(raw_output=raw, analysis=analysis, json_valid=json_valid, schema_valid=schema_valid,
                   evidence_checks_passed=schema_valid and not errors, done_reason=response.get("done_reason"))
        # Rule failures in a well-formed answer (e.g. null purpose without unknowns) can be repaired.
        rule_errors = list(errors) if schema_valid else []
        if response.get("requested_schema"):
            row["requested_schema"] = response["requested_schema"]
            row["requested_schema_hash"] = response["requested_schema_hash"]
        finished = response.get("done") is True and response.get("done_reason") in {None, "stop"}
        if not finished:
            errors.append("Generation was incomplete or stopped at a limit.")
        if message.get("tool_calls"):
            errors.append("Unexpected tool call; the lab does not execute tools.")
        if not demo and schema_valid and finished and not message.get("tool_calls"):
            # Program-side cleanup and claim checks are separate from the structural "valid" status.
            cleaned = clean_answer(analysis)
            if cleaned:
                row["cleaned"] = cleaned
            # Empty inputs/outputs/usage of library code are filled from program facts, not by another request.
            filled = fill_from_facts(analysis, case)
            if filled:
                row["filled"] = filled
            issues = check_claims(analysis, case)
            purpose_flagged = any(issue["field"] == "purpose" for issue in issues)
            issues += [{"field": "format", "item": None, "problem": error} for error in rule_errors
                       if not (purpose_flagged and error.startswith("Unknown purpose"))]
            if needs_repair(issues) and config.get("repair_attempts") and hasattr(client, "repair"):
                repair = repair_once(client, tag, case, raw, analysis, issues, progress)
                if repair["used"]:
                    row.update(first_raw_output=raw, raw_output=repair.pop("raw_output"), analysis=repair["analysis"])
                    issues = repair["issues_after"]
                    row.pop("filled", None)
                    if repair.get("filled"):
                        row["filled"] = repair["filled"]
                    if rule_errors:
                        row.update(first_errors=rule_errors, evidence_checks_passed=True)
                        errors = [error for error in errors if error not in rule_errors]
                repair.pop("analysis", None)
                row["repair"] = repair
                row["wall_ms"] = round((time.perf_counter() - start) * 1000, 2)
            row["check_issues"] = issues
            row["claim_checks_passed"] = not issues
        if not demo:
            row["prompt_tokens"] = numeric(response, "prompt_eval_count")
            row["output_tokens"] = numeric(response, "eval_count")
            # Reading the input and writing the answer are timed separately to see where CPU time goes.
            for original, key in (("load_duration", "load_ms"), ("prompt_eval_duration", "read_ms"),
                                  ("eval_duration", "write_ms"), ("total_duration", "ollama_total_ms")):
                n = numeric(response, original)
                row[key] = round(n / 1e6, 2) if n is not None else None
            duration = numeric(response, "eval_duration")
            row["tokens_per_second"] = round(row["output_tokens"] / (duration / 1e9), 2) if duration and row["output_tokens"] is not None else None
            if row["prompt_tokens"] is not None and row["output_tokens"] is not None:
                if row["prompt_tokens"] + row["output_tokens"] >= config["num_ctx"]:
                    errors.append("Context budget reached; shorten input or raise num_ctx for every candidate.")
            try:
                allocation = client.match(client.running(), tag)
                if allocation:
                    size = numeric(allocation, "size")
                    row["ollama_allocated_mib"] = round(size / 1024**2, 2) if size is not None else None
                    row["size_vram_bytes"] = numeric(allocation, "size_vram")
                    row["cpu_verified"] = row["size_vram_bytes"] == 0 if row["size_vram_bytes"] is not None else None
                if config["device"] == "cpu" and row["cpu_verified"] is not True:
                    errors.append("CPU-only inference could not be verified using /api/ps.")
            except LabError as error:
                errors.append(f"Resource snapshot failed: {error}")
        row["status"] = "valid" if not errors else "invalid_output"
        row["error"] = "; ".join(errors)
    except LabError as error:
        row.update(status=error.code, error=str(error))
        row["wall_ms"] = None if demo else round((time.perf_counter() - start) * 1000, 2)
    finally:
        if progress:
            progress.stop()
            row.update(progress.summary())
    return row


def run_label(models, device, suites, files):
    """Readable end of a result folder name, such as qwen3.5-4b_cpu_118files or qwen3.5-4b_cpu_holdout-12files."""
    def short(tag):
        name, _, size = tag.partition(":")
        size = re.split(r"-(?:instruct|it|q\d)", size)[0]
        return re.sub(r"[^\w.-]+", "-", f"{name}-{size}" if size else name).strip("-")[:40]
    count = f"{files}file" + ("s" if files != 1 else "")
    if len(suites) == 1 and suites[0] in {"dev", "holdout", "robustness"}:
        count = f"{suites[0]}-{count}"
    if not models:  # demo runs: the folder name already starts with demo-
        return count
    model = short(models[0]) if len(models) == 1 else "+".join(map(short, models)) if len(models) == 2 else f"{len(models)}models"
    return f"{model}_{'cpu' if device == 'cpu' else 'gpu'}_{count}"


def run_experiment(client, config, models, cases, output_root, repeat=1, allow_pull=False, demo=False, simple=False):
    run_started = time.perf_counter()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    suites = sorted({case.get("suite", "custom") for case in cases})
    label = run_label([] if demo else models, config["device"], suites, len(cases))
    folder = output_root / (("demo-" if demo else "") + run_id + "_" + label)
    folder.mkdir(parents=True, exist_ok=False)
    metadata = {
        "run_id": run_id, "started_at": utc_now(), "mode": "demo" if demo else "ollama",
        "status": "running", "config": config, "models": models, "repeat": repeat,
        "suites": suites,
        "expected_records": len(models) * len(cases) * repeat,
        "schema_version": SCHEMA_VERSION, "schema_hash": digest(json.dumps(SCHEMA, sort_keys=True)),
        "prompt_hash": digest(PROMPT), "input_version": INPUT_VERSION, "claim_checks_version": CHECKS_VERSION,
        "git_revision": git_revision(),
        "hardware": {"os": platform.platform(), "cpu": platform.processor(), "python": platform.python_version(),
                     "logical_cpus": os.cpu_count(),
                     "performance_cores": performance_cores() if platform.system() == "Darwin" else None},
        "model_details": [], "errors": [],
        "live_metrics": {"enabled": simple and not demo, "refresh_seconds": 1,
                         "resource_scope": "whole machine, including other applications",
                         "ram_method": "physical RAM used; macOS excludes free and inactive pages",
                         "live_speed": "estimate from nonempty streamed chunks over the last five seconds",
                         "final_speed": "Ollama eval_count divided by eval_duration"},
    }
    save_json(folder / "run.json", metadata)
    save_json(folder / "inputs.json", cases)
    (folder / "prompt.txt").write_text(PROMPT + "\n", encoding="utf-8")
    save_json(folder / "schema.json", SCHEMA)
    (folder / "results.jsonl").touch()
    records, aborted, interrupted = [], False, False
    where = "CPU only" if config["device"] == "cpu" else "GPU allowed"
    if config["device"] == "cpu":
        where += f", threads: {config['num_thread']}"
    print(f"Testing {len(cases)} code files ({where})." if simple else f"Results folder: {folder}", flush=True)
    try:
        metadata["ollama_version"] = client.version()
        for model_index, tag in enumerate(models):
            prepared = False
            fatal = False
            try:
                print(f"\nModel: {tag}" if simple else f"\nModel {model_index + 1}/{len(models)}: {tag}", flush=True)
                details = client.prepare(tag, allow_pull)
                prepared = True
                metadata["model_details"].append(details)
                for iteration in range(1, repeat + 1):
                    for sample_index, case in enumerate(cases):
                        print(f"  {sample_index + 1}/{len(cases)}  {case['name']} ..." if simple else f"  [{iteration}/{repeat}] {case['name']} ...", flush=True)
                        row = result_for(client, tag, case, config, demo, live=simple)
                        row.update(record_id=f"m{model_index+1}-r{iteration}-s{sample_index+1}",
                                   model=tag, sample=case["name"], repeat=iteration,
                                   source_hash=case.get("source_hash"), model_digest=details.get("digest"), demo=demo)
                        row.update({key: case.get(key) for key in ("case_id", "suite", "category", "difficulty", "family", "dataset_version", "manifest_hash")})
                        records.append(row)
                        with (folder / "results.jsonl").open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                            stream.flush()
                        if simple:
                            status = "Explanation ready" if row["status"] == "valid" else friendly_error(
                                row.get("error_code") or row["status"], row.get("error", ""))
                            metrics = []
                            if row.get("wall_ms") is not None:
                                metrics.append(clock_text(row["wall_ms"] / 1000))
                            if row.get("tokens_per_second") is not None:
                                metrics.append(f"{row['tokens_per_second']:.1f} tok/s")
                            print("    " + status + (" | " + " | ".join(metrics) if metrics else ""), flush=True)
                        else:
                            print(f"    {row['status']}" + (f": {row['error']}" if row.get("error") else ""), flush=True)
                        if row["status"] in {"timeout", "connection_error", "response_error"}:
                            # A timed-out HTTP call may still be computing server-side.
                            fatal = aborted = True
                            metadata["errors"].append("Stopped after a transport failure; inspect Ollama before retrying.")
                            break
                    if aborted:
                        break
            except LabError as error:
                metadata["errors"].append(f"{tag}: {error}")
                print("  " + friendly_error(error.code, str(error)) if simple else f"  ERROR: {error}", flush=True)
                if error.code in {"timeout", "connection_error", "busy", "response_error"}:
                    fatal = aborted = True
            except KeyboardInterrupt:
                # Do not send a second request while interrupted inference may still run.
                fatal = True
                raise
            finally:
                if prepared and not fatal:
                    try:
                        client.unload(tag)
                    except LabError as error:
                        metadata["errors"].append(f"Could not unload {tag}: {error}")
                        aborted = True
            if aborted:
                break
    except KeyboardInterrupt:
        interrupted = aborted = True
        metadata["errors"].append("Interrupted by user. Partial results retained; inspect Ollama before retrying.")
    except LabError as error:
        aborted = True
        metadata["errors"].append(str(error))
        print(friendly_error(error.code, str(error)) if simple else f"ERROR: {error}", flush=True)
    finally:
        has_errors = bool(metadata["errors"]) or any(r["status"] != "valid" for r in records)
        complete = len(records) == metadata["expected_records"]
        metadata["status"] = "interrupted" if interrupted else "aborted" if aborted else "completed_with_errors" if has_errors or not complete else "completed"
        metadata["finished_at"] = utc_now()
        metadata["wall_ms"] = None if demo else round((time.perf_counter() - run_started) * 1000, 2)
        save_json(folder / "run.json", metadata)
        regenerate(folder)
    if simple:
        if metadata["wall_ms"] is not None:
            print(f"\nTotal time: {clock_text(metadata['wall_ms'] / 1000)}", flush=True)
        print(f"Results saved: {folder / 'report.html'}", flush=True)
    else:
        print(f"\nOpen {folder / 'report.html'}\nTechnical records: {folder / 'summary.md'}\nFill {folder / 'review.csv'} for human quality scores.", flush=True)
    return folder, 130 if interrupted else 0 if metadata["status"] == "completed" else 1
