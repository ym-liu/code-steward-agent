"""Explain one code file with the local model, then check the answer and correct it at most once.

AnalysisService calls result_for() for every new or changed code file.
"""
import time

from ..common import AIError
from .answer import validate_analysis
from .checks import LIST_FIELDS, check_claims, clean_answer, fill_from_facts, needs_repair, repair_message
from .request import source_view


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


def repair_once(client, tag, case, raw, first, issues):
    """Ask once for a corrected answer; keep it only if it is valid, has fewer problems and loses nothing else."""
    started = time.perf_counter()
    feedback = repair_message(issues)
    try:
        response = client.repair(tag, case, raw, feedback)
    except AIError as error:
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


def result_for(client, tag, case, config):
    """Explain one file: the model's answer, format checks, program checks and at most one correction.

    Returns a row: status "valid" with the analysis, tokens and timing, or the reason it failed."""
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
    try:
        response = client.analyze(tag, case)
        row["wall_ms"] = round((time.perf_counter() - start) * 1000, 2)
        message = response.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise AIError("Ollama returned no text message.", "response_error")
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
            errors.append("Unexpected tool call; the program does not execute tools.")
        if schema_valid and finished and not message.get("tool_calls"):
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
                repair = repair_once(client, tag, case, raw, analysis, issues)
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
        # Tokens, timing and the CPU check from Ollama's answer.
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
                errors.append("Context budget reached; the file is too long for num_ctx.")
        try:
            allocation = client.match(client.running(), tag)
            if allocation:
                size = numeric(allocation, "size")
                row["ollama_allocated_mib"] = round(size / 1024**2, 2) if size is not None else None
                row["size_vram_bytes"] = numeric(allocation, "size_vram")
                row["cpu_verified"] = row["size_vram_bytes"] == 0 if row["size_vram_bytes"] is not None else None
            if config["device"] == "cpu" and row["cpu_verified"] is not True:
                errors.append("CPU-only inference could not be verified using /api/ps.")
        except AIError as error:
            errors.append(f"Resource snapshot failed: {error}")
        row["status"] = "valid" if not errors else "invalid_output"
        row["error"] = "; ".join(errors)
    except AIError as error:
        row.update(status=error.code, error=str(error))
        row["wall_ms"] = round((time.perf_counter() - start) * 1000, 2)
    return row
