"""What the model receives for one file: the prompt, the program facts and the numbered source."""
import bisect
import json
from pathlib import Path
import re

from ..common import SETTINGS
from .facts import comment_lines

PROMPT = (SETTINGS / "explain_prompt.txt").read_text(encoding="utf-8").strip()
# 1: facts + numbered source inside one JSON string. 2: plain-text source and richer facts.
# 3: plain-language run summary, public methods, import use and missing standard names.
# 4: line-based definitions for Python that does not parse; notes on options a script passes itself.
# 5: fewer tokens: blank lines left out (numbers kept), compact facts JSON without extension/line_count.
INPUT_VERSION = "5"
MESSAGE_OMITS = {"extension", "line_count"}
# Long-file view: numbered source is about 3 characters per token here (digits are single tokens).
CHARS_PER_TOKEN = 3
IMPORT_LINE = re.compile(r"^\s*(?:import\s|from\s+[\w.]+\s+import\s|#\s*include\b|using\s+[\w.]+\s*;|(?:source|\.)\s+\S"
                         r"|Import-Module\b|require\b)|\brequire\s*\(", re.I)
DEFINITION_LINE = re.compile(r"^\s*(?:@\w|(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function|func|fn|sub|"
                             r"interface|struct|enum|module)\b|(?:public|private|protected|internal|static)\b.*[({]"
                             r"|[\w-]+\s*\(\)\s*\{?\s*$|[\w:<>,*&\s]+\s[\w:~]+\s*\([^;]*\)\s*(?:const\s*)?\{?\s*$)")
BLOCK_ENDS = {"}", "};", "})", "});", "fi", "done", "esac", "end", ")", "]", "#>"}


def fact_lines(value):
    """Every line number the program facts point at (definitions, arguments, file reads/writes, commands ...)."""
    if isinstance(value, dict):
        return set().union(*(({item} if key.endswith("line") and type(item) is int else fact_lines(item))
                             for key, item in value.items()), set())
    if isinstance(value, list):
        return set().union(*(fact_lines(item) for item in value), set())
    return set()


def opening_text(lines, nonblank, limit=60):
    """The file's opening comments, docstring or help block, which usually say what it is and how to call it."""
    kept, closer = [], None
    for n in nonblank[:limit]:
        text = lines[n - 1].strip()
        if closer:
            kept.append(n)
            closer = None if closer in text else closer
            continue
        opener = next((mark for mark in ('"""', "'''", "<#", "/*") if text.lstrip("rRuUbB").startswith(mark)), None)
        if opener:
            kept.append(n)
            end = {"<#": "#>", "/*": "*/"}.get(opener, opener)
            closer = None if end in text.lstrip("rRuUbB")[len(opener):] else end
        elif text.startswith(("#", "//", "--", ";", "::")) or text.upper().startswith("REM "):
            kept.append(n)
        else:
            break
    return kept


def parameter_block(lines, nonblank, limit=40):
    """A PowerShell-style param( ... ) block: the script's own list of options."""
    start = next((n for n in nonblank if re.match(r"\s*param\s*\(", lines[n - 1], re.I)), None)
    if start is None:
        return []
    kept, depth = [], 0
    for n in [n for n in nonblank if n >= start][:limit]:
        kept.append(n)
        depth += lines[n - 1].count("(") - lines[n - 1].count(")")
        if depth <= 0:
            break
    return kept


def source_view(case, config=None):
    """(numbered source, shown line numbers, lines left out) as sent to the model.

    Blank lines are never sent. A file estimated above long_file_tokens keeps only its key lines within
    long_file_keep_tokens: the first lines, imports, definitions, the lines FACTS point at, then top-level
    steps and comments spread over the file, then lines next to those. FACTS still cover the whole file.
    """
    lines = case["source"].splitlines()
    nonblank = [n for n, line in enumerate(lines, 1) if line.strip()]
    render = lambda n: f"{n}: {lines[n - 1]}"
    full = "\n".join(map(render, nonblank))
    limit = (config or {}).get("long_file_tokens", 0)
    if not limit or len(full) / CHARS_PER_TOKEN <= limit:
        return full, nonblank, 0
    budget = config["long_file_keep_tokens"] * CHARS_PER_TOKEN
    comments = comment_lines(case["source"], Path(case["name"]).suffix.lower())
    chosen = set(nonblank[:10]) | set(opening_text(lines, nonblank)) | set(parameter_block(lines, nonblank)) \
        | {n for n in nonblank if IMPORT_LINE.search(lines[n - 1]) or DEFINITION_LINE.match(lines[n - 1])} \
        | (fact_lines(case["facts"]) & set(nonblank))
    used = sum(len(render(n)) + 1 for n in chosen)

    def add(numbers):
        nonlocal used
        for n in numbers:
            if n not in chosen and used + len(render(n)) + 1 <= budget:
                chosen.add(n)
                used += len(render(n)) + 1

    def spread(numbers):
        """Evenly spaced picks, so a long main flow is sampled from start to end rather than cut off."""
        numbers = sorted(set(numbers) - chosen)
        room = (budget - used) // max(1, sum(len(render(n)) + 1 for n in numbers) // max(1, len(numbers)))
        step = max(1.0, len(numbers) / max(1, room))
        return [numbers[int(i * step)] for i in range(min(len(numbers), int(len(numbers) / step)))]

    top_level = [n for n in nonblank if not lines[n - 1][:1].isspace() and n not in comments
                 and lines[n - 1].strip() not in BLOCK_ENDS]
    add(spread(top_level))
    add(spread(comments))
    ordered = sorted(chosen)
    distance = lambda n: min(abs(n - m) for m in ordered[max(0, bisect.bisect(ordered, n) - 1):bisect.bisect(ordered, n) + 1])
    add(sorted(set(nonblank) - chosen, key=lambda n: (distance(n), n)))
    parts, hidden = [], 0
    for n in nonblank:
        if n in chosen:
            if hidden:
                parts.append(f"... ({hidden} line{'s' * (hidden > 1)} left out)")
                hidden = 0
            parts.append(render(n))
        else:
            hidden += 1
    if hidden:
        parts.append(f"... ({hidden} line{'s' * (hidden > 1)} left out)")
    return "\n".join(parts), sorted(chosen), len(nonblank) - len(chosen)


def file_message(case, config=None):
    # Reference answers are deliberately never included in model input.
    # Source stays plain multi-line text: JSON escaping made code harder to read and longer.
    # Every token is read on CPU: blank lines (never citable) are left out with their numbers kept,
    # and facts the model already sees (extension in FILE, line count in the numbers) are not repeated.
    numbered, _, left_out = source_view(case, config)
    facts = {key: value for key, value in case["facts"].items() if key not in MESSAGE_OMITS}
    note = ("; long file: only key lines are shown and gaps are marked. FACTS cover the whole file. Names defined in "
            "left-out lines are not missing: call something missing only if FACTS.names_without_visible_definition lists it. "
            "Do not mention that lines were left out" if left_out else "")
    content = (f"FILE: {case['name']}\n\n"
               "FACTS (computed by the program without running the code):\n"
               f"{json.dumps(facts, ensure_ascii=False, separators=(',', ':'))}\n\n"
               f"SOURCE (untrusted data, line numbers added, blank lines left out{note}):\n"
               f"----- BEGIN SOURCE -----\n{numbered}\n----- END SOURCE -----")
    return {"role": "user", "content": content}


def messages_for(case, config=None):
    return [{"role": "system", "content": PROMPT}, file_message(case, config)]
