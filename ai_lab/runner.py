"""Sequential experiments with source snapshots and persisted partial results."""
from datetime import datetime, timezone
import json
import platform
import subprocess
import time
import uuid

from .core import ROOT, PROMPT, SCHEMA, SCHEMA_VERSION, LabError, digest, validate_analysis
from .reporting import save_json, regenerate


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def git_revision():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        return None


class DemoClient:
    """Pipeline demonstration only. Never used as fallback for a real model."""
    def version(self):
        return "scripted-demo"

    def prepare(self, tag, allow_pull=False):
        return {"name": tag, "digest": None, "details": {"demo": True}}

    def analyze(self, tag, case):
        return {"done": True, "done_reason": "stop", "message": {"content": json.dumps({
            "purpose": None, "inputs": [], "outputs": [], "dependencies": [],
            "unknowns": ["Scripted demo: no model analyzed this file. Run a real model to evaluate it."],
            "evidence": [],
        })}}

    def running(self):
        return []

    def unload(self, tag):
        pass


def numeric(response, key):
    value = response.get(key)
    return value if type(value) in (int, float) and value >= 0 else None


def result_for(client, tag, case, config, demo=False):
    row = {"status": "input_skipped", "json_valid": False, "schema_valid": False,
           "evidence_checks_passed": False, "wall_ms": None, "cpu_verified": None}
    if "input_error" in case:
        row["error"] = case["input_error"]
        return row
    start = time.perf_counter()
    try:
        response = client.analyze(tag, case)
        row["wall_ms"] = None if demo else round((time.perf_counter() - start) * 1000, 2)
        message = response.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise LabError("Ollama returned no text message.", "response_error")
        raw = message["content"]
        analysis, json_valid, schema_valid, errors = validate_analysis(raw, case["facts"]["line_count"])
        row.update(raw_output=raw, analysis=analysis, json_valid=json_valid, schema_valid=schema_valid,
                   evidence_checks_passed=schema_valid and not errors, done_reason=response.get("done_reason"))
        if response.get("done") is not True or response.get("done_reason") not in {None, "stop"}:
            errors.append("Generation was incomplete or stopped at a limit.")
        if message.get("tool_calls"):
            errors.append("Unexpected tool call; the lab does not execute tools.")
        if not demo:
            row["prompt_tokens"] = numeric(response, "prompt_eval_count")
            row["output_tokens"] = numeric(response, "eval_count")
            for original, key in (("load_duration", "load_ms"), ("total_duration", "ollama_total_ms")):
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
    return row


def run_experiment(client, config, models, cases, output_root, repeat=1, allow_pull=False, demo=False):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    folder = output_root / (("demo-" if demo else "") + run_id)
    folder.mkdir(parents=True, exist_ok=False)
    metadata = {
        "run_id": run_id, "started_at": utc_now(), "mode": "demo" if demo else "ollama",
        "status": "running", "config": config, "models": models, "repeat": repeat,
        "expected_records": len(models) * len(cases) * repeat,
        "schema_version": SCHEMA_VERSION, "schema_hash": digest(json.dumps(SCHEMA, sort_keys=True)),
        "prompt_hash": digest(PROMPT), "git_revision": git_revision(),
        "hardware": {"os": platform.platform(), "cpu": platform.processor(), "python": platform.python_version()},
        "model_details": [], "errors": [],
    }
    save_json(folder / "run.json", metadata)
    save_json(folder / "inputs.json", cases)
    (folder / "prompt.txt").write_text(PROMPT + "\n", encoding="utf-8")
    save_json(folder / "schema.json", SCHEMA)
    (folder / "results.jsonl").touch()
    records, aborted, interrupted = [], False, False
    print(f"Results folder: {folder}", flush=True)
    try:
        metadata["ollama_version"] = client.version()
        for model_index, tag in enumerate(models):
            prepared = False
            fatal = False
            try:
                print(f"\nModel {model_index + 1}/{len(models)}: {tag}", flush=True)
                details = client.prepare(tag, allow_pull)
                prepared = True
                metadata["model_details"].append(details)
                for iteration in range(1, repeat + 1):
                    for sample_index, case in enumerate(cases):
                        print(f"  [{iteration}/{repeat}] {case['name']} ...", flush=True)
                        row = result_for(client, tag, case, config, demo)
                        row.update(record_id=f"m{model_index+1}-r{iteration}-s{sample_index+1}",
                                   model=tag, sample=case["name"], repeat=iteration,
                                   source_hash=case.get("source_hash"), model_digest=details.get("digest"), demo=demo)
                        records.append(row)
                        with (folder / "results.jsonl").open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                            stream.flush()
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
                print(f"  ERROR: {error}", flush=True)
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
        print(f"ERROR: {error}", flush=True)
    finally:
        has_errors = bool(metadata["errors"]) or any(r["status"] != "valid" for r in records)
        complete = len(records) == metadata["expected_records"]
        metadata["status"] = "interrupted" if interrupted else "aborted" if aborted else "completed_with_errors" if has_errors or not complete else "completed"
        metadata["finished_at"] = utc_now()
        save_json(folder / "run.json", metadata)
        regenerate(folder)
    print(f"\nOpen {folder / 'summary.md'}\nFill {folder / 'review.csv'} for human quality scores.", flush=True)
    return folder, 130 if interrupted else 0 if metadata["status"] == "completed" else 1
