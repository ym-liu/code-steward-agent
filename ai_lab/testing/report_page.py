"""Short, standalone reports for people; model text is always escaped."""
import html
from pathlib import Path


def friendly_error(code, detail=""):
    if code == "input_too_large" or "Context budget" in detail:
        return "This file exceeds the reading limit and was not completed."
    messages = {
        "input_empty": "This file is empty and was skipped.",
        "input_binary": "This is not a plain-text code file and was skipped.",
        "input_encoding": "This file could not be read as UTF-8 text.",
        "input_link": "This is a file link. Add the actual code file instead.",
        "input_unreadable": "This file could not be read. Check that it exists and is accessible.",
        "connection_error": "Cannot connect to Ollama. Open Ollama, then run the test again.",
        "missing_model": "This model is not installed. Connect to the internet and try again.",
        "busy": "Another model is loaded in Ollama. Stop the other test first.",
        "timeout": "The model took too long. This run stopped; check Ollama before retrying.",
        "response_error": "The model connection failed. This run stopped; check Ollama.",
        "cloud_model": "This model uses a cloud service. Choose a local model.",
        "model_error": "The model did not return a result. Check Ollama.",
        "not_tested": "This file was not tested because the run did not finish.",
        "invalid_output": "The response failed format or code-reference checks and is incomplete.",
    }
    if code == "invalid_output" and ("limit" in detail or "incomplete" in detail):
        return "The model stopped before finishing its explanation."
    return messages.get(code, "The test did not finish. Details are saved in the result folder.")


def duration(ms):
    if ms is None:
        return ""
    seconds = ms / 1000
    return f"{seconds:.1f} seconds" if seconds < 60 else f"{seconds / 60:.1f} minutes"


def entries_for(metadata, inputs, records):
    """Show missing work too, including failure before the first file."""
    lookup = {(row["model"], row["sample"], row["repeat"]): row for row in records}
    entries = []
    for model in metadata["models"]:
        for repeat in range(1, metadata.get("repeat", 1) + 1):
            for case in inputs:
                entries.append(lookup.get((model, case["name"], repeat), {
                    "model": model, "sample": case["name"], "repeat": repeat,
                    "status": "not_tested", "error": "", "wall_ms": None,
                }))
    return entries


def write_readable_reports(folder, metadata, inputs, records):
    folder = Path(folder)
    escape = lambda value: html.escape(str(value), quote=True)
    cases = {case["name"]: case for case in inputs}
    entries = entries_for(metadata, inputs, records)
    good = sum(row["status"] == "valid" for row in entries)
    demo = metadata["mode"] == "demo"
    intro = "DEMO ONLY: no real AI model was used." if demo else "These explanations are AI-generated. Check important claims. Your code was read, not executed."
    remaining = len(entries) - good
    count = f"{good} explanation{'s' if good != 1 else ''} ready; {remaining} file{'s' if remaining != 1 else ''} not completed."
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             '<title>Code Test Results</title>',
             '<style>body{font:16px/1.7 system-ui,sans-serif;color:#24313c;background:#f3f5f7;margin:0}'
             'main{max-width:880px;margin:36px auto;padding:0 20px}h1{font-size:28px}h2{font-size:21px;margin:0}'
             'article{background:white;padding:24px;margin:20px 0;border:1px solid #dbe2e8;border-radius:12px}'
             '.muted{color:#617080;font-size:14px}.status{float:right;color:#17654c;font-size:14px}'
             '.problem{color:#965514;background:#fff5e6;padding:12px;border-radius:8px}'
             'dl{display:grid;grid-template-columns:112px 1fr;gap:8px 16px}dt{font-weight:600}dd{margin:0}'
             'ul{margin:0;padding-left:22px}details{border-top:1px solid #edf0f3;margin-top:16px;padding-top:12px}'
             'summary{cursor:pointer;color:#375c78}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f9;padding:12px;font-size:13px}'
             'p,dd,h2{overflow-wrap:anywhere}a{color:#375c78}@media(max-width:550px){dl{display:block}dt{margin-top:12px}.status{float:none;display:block}}</style>',
             '<main><h1>Code Test Results</h1>', f'<p>{escape(count)}</p><p class="muted">{escape(intro)}</p>']
    lines = ["# Code Test Results", "", count, "", intro, ""]
    if not demo and metadata.get("wall_ms") is not None:
        total = f"Total time: {duration(metadata['wall_ms'])} (including model setup)."
        parts.append(f'<p class="muted">{escape(total)}</p>')
        lines += [total, ""]
    if not demo and metadata.get("config", {}).get("device", "cpu") != "cpu":
        gpu = "GPU was allowed for this preview run. Answers and speed can differ from the CPU-only setting."
        parts.append(f'<p class="problem">{escape(gpu)}</p>')
        lines += [gpu, ""]
    if not entries:
        parts.append('<p class="problem">No code files were found.</p>')
    for row in entries:
        case = cases[row["sample"]]
        status = "Ready" if row["status"] == "valid" else "Not completed"
        timing = duration(row.get("wall_ms"))
        speed = row.get("tokens_per_second")
        if speed is not None and not demo:
            timing += (" · " if timing else "") + f"{speed:.1f} tok/s"
        parts += [f'<article><span class="status">{status}</span><h2>{escape(row["sample"])}</h2>',
                  f'<p class="muted">{escape(row["model"])}' + (f' · {escape(timing)}' if timing else '') + '</p>']
        lines += [f'## {row["sample"]}', "", f'Model: {row["model"]}. {status}. ' + (f'Time: {timing}.' if timing else ''), ""]
        if row["status"] != "valid":
            error = friendly_error(row.get("error_code") or row["status"], row.get("error", ""))
            if row["status"] == "input_skipped":
                error = friendly_error(case.get("input_error_code", "input_unreadable"), row.get("error", ""))
            parts += [f'<p class="problem">{escape(error)}</p>', '</article>']
            lines += [error, ""]
            continue
        analysis = row.get("analysis") or {}
        purpose = analysis.get("purpose") or "The main purpose could not be determined."
        parts += [f'<p><strong>{escape(purpose)}</strong></p>', '<dl>']
        lines += [purpose, ""]
        for key, label in (("inputs", "Inputs"), ("outputs", "Outputs"), ("usage", "How to use"),
                           ("dependencies", "Dependencies"), ("relationships", "Related files")):
            items = analysis.get(key) or []
            rendered = '<ul>' + ''.join(f'<li>{escape(item)}</li>' for item in items) + '</ul>' if items else 'Not listed.'
            parts.append(f'<dt>{label}</dt><dd>{rendered}</dd>')
            lines += [f'**{label}:** ' + ('; '.join(items) if items else 'Not listed.'), ""]
        parts.append('</dl>')
        unknowns = analysis.get("unknowns") or []
        if unknowns:
            parts.append('<div class="problem"><strong>Check before use</strong><ul>' + ''.join(f'<li>{escape(item)}</li>' for item in unknowns) + '</ul></div>')
            lines += ["**Check before use:** " + '; '.join(unknowns), ""]
        flags = [f'{issue["field"]}: {issue["problem"]}' for issue in row.get("check_issues") or []]
        if flags:
            parts.append('<div class="problem"><strong>Needs checking (found by the program)</strong><ul>' + ''.join(f'<li>{escape(item)}</li>' for item in flags) + '</ul></div>')
            lines += ["**Needs checking (found by the program):** " + '; '.join(flags), ""]
        if (row.get("repair") or {}).get("used"):
            note = "The program found problems in the first answer; this is the corrected answer."
            parts.append(f'<p class="muted">{escape(note)}</p>')
            lines += [note, ""]
        view = row.get("source_view")
        if view:
            note = (f"Long file: the model read its {view['shown_lines']} key lines (imports, definitions, main steps); "
                    f"{view['left_out_lines']} other lines were left out to save time. The program checks used the whole file.")
            parts.append(f'<p class="muted">{escape(note)}</p>')
            lines += [note, ""]
        filled = sorted({item["field"] for item in row.get("filled") or []})
        if filled:
            note = ("The model left these parts empty, so the program filled them from the code: "
                    + ", ".join("how to use" if field == "usage" else field for field in filled) + ".")
            parts.append(f'<p class="muted">{escape(note)}</p>')
            lines += [note, ""]
        evidence = analysis.get("evidence") or []
        source = case.get("source", "").splitlines()
        if evidence:
            parts.append('<details><summary>Show code evidence</summary>')
            for item in evidence:
                number = item.get("line")
                quote = source[number - 1] if type(number) is int and 1 <= number <= len(source) else '(code location not found)'
                parts += [f'<p>Line {escape(number)}: {escape(item.get("claim", ""))}</p><pre>{escape(quote)}</pre>']
            parts.append('</details>')
        parts.append('</article>')
    if metadata.get("errors"):
        parts.append('<p class="problem">Some tests did not finish. Completed explanations are kept here.</p>')
    parts.append('<details><summary>Show detailed test records</summary><p>Raw answers, source snapshots and the optional review sheet are saved in the same result folder.</p><a href="summary.md">Open technical records</a></details></main></html>')
    (folder / "report.html").write_text('\n'.join(parts) + '\n', encoding="utf-8")
    (folder / "report.md").write_text('\n'.join(lines) + '\n', encoding="utf-8")
