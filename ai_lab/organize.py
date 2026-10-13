"""Classify files and propose an organization plan. This is a dry run: nothing is copied, moved or deleted.

Step one labels every file in the tested folder (type, role, language, sensitive flag, exact duplicates and
a confidence computed by the program) from file contents, program facts and the per-file explanations of a
finished run. Step three keeps files that use each other together, asks the model for one topic per group
from a fixed list (program keyword hints are the fallback and the agreement check) and proposes folders.
Grouping versions and variants of one tool is a later step.
"""
import hashlib
import html
import json
import os
import posixpath
import re
import time
import ast
from collections import Counter, defaultdict
from pathlib import Path

from .common import ANSWER_KEYS, SAMPLES, SETTINGS, LabError, read_json, save_json
from .explain.facts import comment_lines, parse_python, static_facts
from .explain.files import EXCLUDED, EXTENSIONS
from .explain.ollama_client import OllamaClient

ORGANIZE_VERSION = "1"
TOPIC_PROMPT = (SETTINGS / "topic_prompt.txt").read_text(encoding="utf-8").strip()
TYPES = ("wrapper", "script", "binary", "sample", "data", "documentation", "unknown")
OS_JUNK = {".DS_Store", "Thumbs.db", "desktop.ini"}
CODE_EXTENSIONS = EXTENSIONS - {".txt"}
DOC_NAME = re.compile(r"^(?:readme|license|licence|copying|notice|changelog|changes|authors|contributing|history)(?:[._-]|$)", re.I)
DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".adoc", ".pdf", ".doc", ".docx", ".odt", ".rtf", ".tex"}
DATA_EXTENSIONS = {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                   ".conf", ".properties", ".env", ".xlsx", ".xls", ".ods", ".parquet", ".feather", ".h5", ".hdf5",
                   ".npy", ".npz", ".pkl", ".pickle", ".pt", ".pth", ".ckpt", ".onnx", ".safetensors", ".db",
                   ".sqlite", ".sqlite3", ".log", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".svg",
                   ".webp", ".ico", ".mp3", ".wav", ".flac", ".mp4", ".mov", ".avi", ".zip", ".tar", ".gz", ".tgz",
                   ".bz2", ".xz", ".7z", ".rar", ".dat"}
CONFIG_EXTENSIONS = {".env", ".ini", ".cfg", ".conf", ".properties", ".toml", ".yaml", ".yml"}
CONFIG_NAME = re.compile(r"(?:^|[._-])(?:env|config|configuration|settings|secrets?|credentials?)(?:[._-]|$)", re.I)
CONFIG_CODE = re.compile(r"^(?:local[_-])?(?:config|configuration|settings|secrets?|credentials?)$", re.I)
PROSE_WORD = re.compile(r"(?<!\S)[a-z][a-z'-]*[a-z](?!\S)")
KEY_FILE = re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)$|\.(?:pem|key|p12|pfx|kdbx|keystore|jks)$|^\.(?:netrc|pgpass|npmrc|pypirc)$", re.I)
SECRET = re.compile(r"""(?i)\b(?:pass(?:word|wd)?|secret(?:_key)?|api[_-]?key|access[_-]?key|auth[_-]?token|token)\b["']?\s*[:=]\s*(["'])([^"'\s]{6,})\1""")
PLACEHOLDER = re.compile(r"(?i)^(?:x+|\*+|\.+|change-?me.*|replace.*|your.*|example.*|dummy.*|placeholder.*|<.*>|\$\{.*\}|%.*%)$")
SAMPLE_FOLDERS = {"sample", "samples", "example", "examples", "demo", "demos", "fixture", "fixtures", "testdata",
                  "test_data", "test-data", "sample_data", "sample-data"}
SAMPLE_NAME = re.compile(r"(?:^|[._-])(?:sample|example|expected|fixture|demo)s?(?:[._-]|$)", re.I)
MARKER = re.compile(r"^(?:readme|license|licence|copying)(?:\.\w+)?$|^(?:package\.json|pyproject\.toml|setup\.py|setup\.cfg|"
                    r"requirements\.txt|go\.mod|cargo\.toml|pom\.xml|build\.gradle|makefile|cmakelists\.txt|gemfile|"
                    r"composer\.json)$|\.(?:sln|csproj)$", re.I)
BINARY_MAGIC = ((b"\x7fELF", "ELF executable (Linux)"), (b"MZ", "Windows executable"),
                (b"\xcf\xfa\xed\xfe", "Mach-O executable (macOS)"), (b"\xce\xfa\xed\xfe", "Mach-O executable (macOS)"),
                (b"\xfe\xed\xfa\xcf", "Mach-O executable (macOS)"), (b"\xfe\xed\xfa\xce", "Mach-O executable (macOS)"),
                (b"\xca\xfe\xba\xbe", "Mach-O universal executable or Java class"))
BINARY_EXTENSIONS = {".exe": "Windows executable", ".dll": "Windows library", ".so": "shared library",
                     ".dylib": "macOS library", ".com": "DOS executable", ".jar": "Java archive",
                     ".class": "Java class", ".msi": "Windows installer", ".o": "object file", ".a": "static library"}
SHEBANG = re.compile(r"^#!\s*\S*?(?:\benv\s+(?:-\S+\s+)*)?\b(python3?|bash|sh|zsh|node|ruby|perl|pwsh|Rscript)\b")
SHEBANG_LANGUAGES = {"python": "Python", "python3": "Python", "bash": "Shell script", "sh": "Shell script",
                     "zsh": "Shell script", "node": "JavaScript", "ruby": "Ruby", "perl": "Perl",
                     "pwsh": "PowerShell script", "Rscript": "R"}
OTHER_LANGUAGES = {".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".csv": "CSV", ".tsv": "TSV", ".md": "Markdown",
                   ".toml": "TOML", ".ini": "INI settings", ".env": "environment settings", ".xml": "XML",
                   ".png": "PNG image", ".jpg": "JPEG image", ".jpeg": "JPEG image", ".pdf": "PDF"}
DATA_LINE = re.compile(r"^(?:https?://\S+|[\w.+-]+@[\w.-]+\.\w+|[-+]?\d[\d.,:;/\s-]*|\S+(?:\s*[,;\t]\s*\S+)+|[\w.~/\\-]+\.\w{1,5})$")
CODE_FILE = r"[\w.~/\\%${}()-]*[\w-]\.(?:py|js|mjs|cjs|ts|r|rb|pl|sh|ps1|bat|cmd|sql|jar|php)\b"
INTERPRETER_RUN = re.compile(r"(?:^|[\s\"'(\[,;&|=])(?:python3?|py|node|rscript|ruby|perl|bash|sh|zsh|pwsh|powershell|java|"
                             rf"php|psql|cmd)(?:\.exe)?\b[^|;\n]*?({CODE_FILE})", re.I)
DIRECT_RUN = re.compile(r"""(?:^|[;&|(]\s*|\b(?:exec|call|start|nohup|source)\s+|^\s*\.\s+)["']?((?:\./|\.\\|%~dp0|\$\{?\w+\}?[/\\]|\$\(dirname[^)]*\)[/\\]|bin[/\\])[\w./\\-]+)""", re.I)
LAUNCH_WORDS = re.compile(r"\b(?:launch(?:es)?|runs?|starts?|calls?|invokes?|wrappers?|executes?|bootstraps?)\b", re.I)
TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)(?:test_[^/]*\.py|conftest\.py|[^/]*_test\.(?:py|go)|[^/]*\.(?:test|spec)\.[jt]sx?)$", re.I)
FROM_OTHER_CODE = re.compile(r"\b(?:from other code|import(?:ed|s)?|require[ds]?|register(?:ed|s)?|activate[sd]?|loaded by|used by|call(?:ed)? from)\b", re.I)
RUN_WORDS = re.compile(r"\b(?:run|runs|execute|executes|start|starts|launch|launches|command line|terminal|go run|rscript)\b|"
                       r"\b(?:python3?|node|ruby|bash|java)\s+\S+", re.I)
JS_EXPORT = re.compile(r"^\s*(?:module\.exports|exports\.\w+|export\s+(?:default|function|class|const|let|interface|async))", re.M)
JS_IMPORT = re.compile(r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*|^\s*import\s+)["'](\.{1,2}/[^"']+)["']""", re.M)
C_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.M)
KNOWN_EXTENSIONS = sorted({e.lstrip(".") for e in CODE_EXTENSIONS | DATA_EXTENSIONS | DOC_EXTENSIONS | {".txt"}}, key=len, reverse=True)
PATH_TOKEN = re.compile(r"[\w.~/\\%${}():-]*[\w-]\.(?:" + "|".join(KNOWN_EXTENSIONS) + r")(?![\w.])", re.I)
VARIABLE_PREFIX = re.compile(r"%~dp0|\$\(dirname[^)]*\)/?|\$\{\w+\}/|\$\w+/|\$PSScriptRoot/", re.I)
COPY_MARK = re.compile(r"(?:^|/)(?:backups?|old|archive|copies)/|copy|\(\d+\)|[._-](?:bak|old|orig|backup)(?:[._-]|$)|~$", re.I)
GENERIC_STEM = re.compile(r"^(?:main|index|app|server|run|start|script|extension|__init__|__main__|init|cli|program|"
                          r"test|tests|untitled\d*|new|temp|tmp)$", re.I)
GENERIC_FOLDERS = SAMPLE_FOLDERS | {"src", "lib", "data", "files", "misc", "stuff"}
FOLDER_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,4}$")
TOPIC_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION_WORDS = re.compile(r"(?:^|-)(?:final|old|new|copy|v\d+|backup|tmp|temp)(?:-|$)")
WORD = re.compile(r"[a-z][a-z0-9]+")
ROLE_WORDS = {"tool": "runs by itself", "module": "used by other code", "test": "test code"}


def say(text):
    print(text, flush=True)


def load_topics(path=None):
    data = read_json(path or SETTINGS / "topics.json")
    topics = data.get("topics") if isinstance(data, dict) else None
    if (not isinstance(topics, list) or len(topics) < 2 or not all(isinstance(t, dict) for t in topics)
            or topics[-1].get("id") != "other" or len({t.get("id") for t in topics}) != len(topics)
            or not all(isinstance(t.get("id"), str) and TOPIC_ID.match(t["id"]) and isinstance(t.get("description"), str)
                       for t in topics)):
        raise LabError("topics.json needs unique lowercase-hyphen ids, each with a description, and 'other' last.", "config_error")
    for topic in topics:
        topic["hints"] = {str(word).lower() for word in topic.get("hints", [])}
    return {"version": str(data.get("version", "1")), "topics": topics}


def parent(path):
    return posixpath.dirname(path) or "."


def under(path, folder):
    return folder == "." or path.startswith(folder + "/")


def within(folder, root):
    return folder == root or under(folder, root)


def common_root(paths):
    folders = [parent(p) for p in paths]
    return "." if "." in folders else posixpath.commonpath(folders) or "."


def slug(text, limit=40):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", text)
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].rstrip("-") or "unnamed"


def file_hash(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            value.update(block)
    return value.hexdigest()


def load_run(folder):
    folder = Path(folder).expanduser().absolute()
    metadata, cases = read_json(folder / "run.json"), read_json(folder / "inputs.json")
    if not isinstance(cases, list) or not cases:
        raise LabError("This result folder has no tested files.", "input_error")
    rows = {}
    results = folder / "results.jsonl"
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            row = json.loads(line) if line.strip() else None
            if row and row.get("repeat", 1) == 1 and row.get("model") == metadata["models"][0]:
                rows.setdefault(row["sample"], row)
    roots = set()
    for case in cases:
        parts, source = Path(case["name"]).parts, Path(case["source_path"])
        if source.parts[-len(parts):] != parts:
            raise LabError("The result folder does not match its tested files.", "input_error")
        roots.add(Path(*source.parts[:-len(parts)]))
    if len(roots) != 1 or not next(iter(roots)).is_dir():
        raise LabError("The tested folder was moved or the run mixes folders; rerun the test first.", "input_error")
    return folder, metadata, cases, rows, roots.pop()


def scan(root, limit):
    found = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if name in OS_JUNK or path.is_symlink():
                continue
            found.append(rel)
            if len(found) > limit:
                raise LabError("Too many files to organize; choose a smaller folder or raise max_files.", "input_error")
    return found


def executable_lines(source, suffix):
    skipped = comment_lines(source, suffix)
    return [(n, line) for n, line in enumerate(source.splitlines(), 1)
            if line.strip() and n not in skipped and not line.startswith("#!")]


def launches(source, suffix):
    """Executable lines that start another script or a program by its path."""
    lines = executable_lines(source, suffix)
    found = []
    for number, line in lines:
        match = INTERPRETER_RUN.search(line) or DIRECT_RUN.search(line)
        if match:
            found.append((number, match.group(1)))
    return found, len(lines)


def explanation(case, row, current_hash):
    if not row or row.get("status") != "valid" or not isinstance(row.get("analysis"), dict):
        return None, "No AI explanation is available for this file."
    if case.get("source_hash") != current_hash:
        return None, "The file changed after the AI explanation; it was not used."
    return row["analysis"], None


def short_dependency(text):
    return re.split(r"\s+\(|:|\s+-\s", str(text), 1)[0].strip()[:30]


def sensitive_reason(name, suffix, text, code):
    if KEY_FILE.search(name):
        return "Private key or password-store file."
    stem = Path(name).stem
    if CONFIG_CODE.match(stem) if code else (suffix in CONFIG_EXTENSIONS or CONFIG_NAME.search(stem)):
        return "Configuration or environment file (sensitive by default)."
    for match in SECRET.finditer(text or ""):
        if not PLACEHOLDER.match(match.group(2)):
            return "Contains what looks like a password, key or token."
    return None


def text_type(text, purpose, explained):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    share = sum(bool(DATA_LINE.match(line)) for line in lines) / len(lines) if lines else 0
    if share >= 0.7:
        return "data", 0.8, "Most lines are values such as web addresses, numbers or comma-separated fields."
    if purpose:
        return "documentation", 0.65, "Readable notes or instructions rather than code or data."
    if lines and sum(len(PROSE_WORD.findall(line)) >= 3 for line in lines) / len(lines) >= 0.3:
        return "documentation", 0.5, "Looks like written notes, although the AI gave no description."
    return "unknown", 0.6 if explained else 0.4, "The text has no recognizable code or document structure."


def code_role(rel, facts, analysis, source, suffix):
    """tool = does work when started; module = used by other code; test = test code."""
    usage = " ".join((analysis or {}).get("usage") or [])
    other, run = bool(FROM_OTHER_CODE.search(usage)), bool(RUN_WORDS.search(usage))
    if TEST_PATH.search(rel):
        return "test", 0.85, "Test file by its name or folder."
    runs = facts.get("runs_when_executed") or ""
    entry = (facts.get("entry_point") or {}).get("kind", "")
    relative = any(name.startswith(".") for name in facts.get("imports", []))
    if runs.startswith("No"):
        return "module", 0.8, "Nothing runs when the file is started; other code uses its functions or classes."
    if runs.startswith("Yes"):
        if entry.startswith("top-level") and (relative or (other and not run)):
            return "module", 0.7, "Only setup code runs at the top; the file is meant to be used by other code."
        return "tool", 0.8, "Does its work when started."
    if other and not run:
        return "module", 0.65, "The AI description says other code uses it."
    if run:
        return "tool", 0.65, "The AI description says it is started directly."
    if suffix in {".js", ".jsx", ".ts", ".tsx"} and JS_EXPORT.search(source):
        return "module", 0.6, "Exports functions or classes for other code."
    return "tool", 0.5, "No clear sign either way; assumed to be started directly."


def classify_file(root, rel, case, row):
    path = root / rel
    name = posixpath.basename(rel)
    suffix = Path(name).suffix.lower() or (name.lower() if name.startswith(".") else "")
    with path.open("rb") as stream:
        head = stream.read(65536)
    record = {"path": rel, "size": path.stat().st_size, "sha256": file_hash(path), "type": "unknown", "role": None,
              "language": None, "sensitive": False, "sensitive_reason": None, "duplicate_of": None,
              "confidence": 0.4, "reasons": [], "summary": None, "dependencies": [], "starts": [], "explained": False,
              "check_notes": 0}
    text = None if b"\x00" in head else head.decode("utf-8", errors="replace")
    magic = next((label for prefix, label in BINARY_MAGIC if head.startswith(prefix)), None)
    if magic and b"\x00" in head[:4096] or suffix in BINARY_EXTENSIONS:
        record.update(type="binary", language=magic or BINARY_EXTENSIONS[suffix], confidence=0.95 if magic else 0.85,
                      reasons=["Executable or compiled file (" + (magic or "by its extension") + "); it is never run."])
        return record, None
    analysis, note = explanation(case, row, record["sha256"]) if case and "facts" in case else (None, None)
    if case and "input_error" in case:
        note = "The AI test skipped this file: " + case["input_error"]
    if note:
        record["reasons"].append(note)
    if analysis:
        record.update(explained=True, summary=analysis.get("purpose"),
                      dependencies=[short_dependency(d) for d in analysis.get("dependencies") or []][:8],
                      check_notes=len(row.get("check_issues") or []))
    source = case.get("source") if case and "source" in case else text
    facts = (case or {}).get("facts") or {}
    if not facts and text is not None and (suffix in CODE_EXTENSIONS or (suffix == "" and SHEBANG.match(text))):
        facts = static_facts(text, suffix if suffix in CODE_EXTENSIONS else ".sh")  # program facts without a run
    language = facts.get("language", "")
    shebang = SHEBANG.match(text or "")
    if case and "facts" in case and not language.startswith("unknown"):
        kind = "code"
    elif suffix in CODE_EXTENSIONS or (shebang and not suffix):
        kind, language = "code", language or SHEBANG_LANGUAGES.get(shebang.group(1) if shebang else "", "") or suffix.lstrip(".")
    else:
        kind = "other"
    if kind == "code":
        record["language"] = language
        found, count = launches(source or "", suffix)
        forwards = any(item.get("note") for item in facts.get("command_line") or [])
        record["starts"] = [target for _, target in found][:8]
        if (found or forwards) and (count <= 15 or len(found) / count >= 0.25):
            lines = ", ".join(str(n) for n, _ in found[:4]) or "the forwarding line"
            record.update(type="wrapper", confidence=0.8,
                          reasons=record["reasons"] + [f"Mainly starts other programs or scripts (line {lines})."])
            if analysis and LAUNCH_WORDS.search(analysis.get("purpose") or ""):
                record["confidence"] += 0.15
                record["reasons"].append("The AI description also says it starts other programs.")
        else:
            role, confidence, reason = code_role(rel, facts, analysis, source or "", suffix)
            usage = " ".join((analysis or {}).get("usage") or [])
            agrees = {"module": FROM_OTHER_CODE, "tool": RUN_WORDS, "test": None}[role]
            record.update(type="script", role=role, confidence=confidence, reasons=record["reasons"] + [reason])
            if analysis and agrees is not None:
                if agrees.search(usage):
                    record["confidence"] += 0.1
                    record["reasons"].append("The AI's usage notes agree.")
                elif (RUN_WORDS if role == "module" else FROM_OTHER_CODE).search(usage):
                    record["confidence"] -= 0.15
                    record["reasons"].append("The AI's usage notes suggest otherwise; check.")
        if not analysis:
            record["confidence"] -= 0.15
        if facts.get("parse_error"):
            record["confidence"] -= 0.1
            record["reasons"].append("Part of the file could not be parsed.")
        if record["check_notes"]:
            record["confidence"] -= 0.05
    else:
        record["language"] = OTHER_LANGUAGES.get(suffix) or ("text" if text is not None else "binary data")
        in_sample_folder = any(part.lower() in SAMPLE_FOLDERS for part in rel.split("/")[:-1])
        if DOC_NAME.match(name) or suffix in DOC_EXTENSIONS:
            record.update(type="documentation", confidence=0.9, reasons=["Documentation by its name or format."])
        elif (suffix in DATA_EXTENSIONS or suffix == ".txt") and (in_sample_folder or SAMPLE_NAME.search(Path(name).stem)):
            record.update(type="sample", confidence=0.7, reasons=["Example data: it sits in an examples folder or is named as one."])
        elif suffix in DATA_EXTENSIONS:
            record.update(type="data", confidence=0.9, reasons=["Data, settings or media file by its format."])
        elif text is None:
            record.update(type="data", confidence=0.6, reasons=["Binary data that is not a program."])
        else:
            kind, confidence, reason = text_type(text, record["summary"], record["explained"])
            record.update(type=kind, confidence=confidence, reasons=record["reasons"] + [reason])
    if record["type"] not in ("binary",):
        reason = sensitive_reason(name, suffix, text, kind == "code")
        if reason:
            record.update(sensitive=True, sensitive_reason=reason)
    record["confidence"] = round(min(0.99, max(0.05, record["confidence"])), 2)
    return record, source if kind == "code" else None


def mark_duplicates(records):
    groups = defaultdict(list)
    for record in records:
        if record["size"] >= 16:
            groups[record["sha256"]].append(record)
    for group in groups.values():
        if len(group) > 1:
            keep = min(group, key=lambda r: (bool(COPY_MARK.search(r["path"])), r["path"].count("/"), len(r["path"]), r["path"]))
            for record in group:
                if record is not keep:
                    record["duplicate_of"] = keep["path"]
                    record["reasons"].append(f"Exact copy of {keep['path']}.")


def resolve(token, folder, files):
    """Map a path written in code to a scanned file: from the file's folder, then up to two parents."""
    token = VARIABLE_PREFIX.sub("", token.replace("\\", "/")).rstrip("}\"')")
    token = re.split(r"[:=({}$%]", token)[-1].lstrip("-")
    while token.startswith("./"):
        token = token[2:]
    if not token or token.startswith("/") or token.startswith("~"):
        return None
    base = folder
    for _ in range(3):
        candidate = posixpath.normpath(token if base == "." else posixpath.join(base, token))
        if candidate in files:
            return candidate
        if base == ".":
            break
        base = parent(base)
    if "/" not in token:
        matches = [f for f in files if posixpath.basename(f) == token and under(f, folder)]
        if len(matches) == 1:
            return matches[0]
    return None


def module_file(name, folder, level, files):
    parts = [part for part in name.split(".") if part]
    if level:
        base = folder
        for _ in range(level - 1):
            base = parent(base)
        bases = [base]
    else:
        bases = list(dict.fromkeys([folder, parent(folder), parent(parent(folder))]))
    for base in bases:
        for k in range(len(parts), 0, -1):
            stem = posixpath.join(*parts[:k]) if base == "." else posixpath.join(base, *parts[:k])
            for candidate in (stem + ".py", stem + "/__init__.py"):
                if candidate in files:
                    return candidate
    return None


def links_for(rel, source, suffix, facts, files):
    """Files this code file names explicitly: imports, started scripts and paths in its code."""
    folder, found, package = parent(rel), set(), False
    if suffix == ".py":
        tree, _ = parse_python(source)
        for node in ast.walk(tree) if tree is not None else []:
            if isinstance(node, ast.Import):
                names, level = [alias.name for alias in node.names], 0
            elif isinstance(node, ast.ImportFrom):
                level = node.level or 0
                package = package or level > 0
                names = [f"{node.module}.{a.name}" if node.module else a.name for a in node.names if a.name != "*"] or [node.module or ""]
            else:
                continue
            for name in names:
                target = module_file(name, folder, level, files)
                if target:
                    found.add(target)
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        for spec in JS_IMPORT.findall(source):
            base = posixpath.normpath(posixpath.join(folder, spec) if folder != "." else spec)
            found.update(base + end for end in ("", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx", "/index.js", "/index.ts")
                         if base + end in files)
    elif suffix in {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp"}:
        found.update(filter(None, (resolve(name, folder, files) for name in C_INCLUDE.findall(source))))
    elif suffix == ".go":
        folders = {parent(f) for f in files if f.endswith(".go")}
        for name in facts.get("imports") or []:
            tail = "/".join(name.split("/")[-2:])
            matches = [d for d in folders if d == tail or d.endswith("/" + tail)]
            if len(matches) == 1:
                found.update(f for f in files if f.endswith(".go") and parent(f) == matches[0])
    for _, line in executable_lines(source, suffix):
        for token in PATH_TOKEN.findall(line) + [m.group(1) for m in DIRECT_RUN.finditer(line)]:
            target = resolve(token, folder, files)
            if target:
                found.add(target)
    found.discard(rel)
    return found, package


def build_units(records, links, packages):
    by_path = {r["path"]: r for r in records}
    paths = sorted(by_path)
    marked = sorted({parent(p) for p in paths if MARKER.match(posixpath.basename(p)) and parent(p) != "."}, key=len)
    project_roots = []
    for folder in marked:
        if not any(within(folder, root) for root in project_roots):
            project_roots.append(folder)
    units = [{"kind": "project", "root": root, "members": [p for p in paths if under(p, root)]} for root in project_roots]
    taken = {p for unit in units for p in unit["members"]}
    leader = {p: p for p in paths if p not in taken}

    def find(p):
        while leader[p] != p:
            leader[p] = leader[leader[p]]
            p = leader[p]
        return p

    def join(a, b):
        if a in leader and b in leader:
            leader[find(a)] = find(b)
    for source, targets in links.items():
        for target in targets:
            join(source, target)
    for source in packages:
        for other in paths:
            if other.endswith(".py") and parent(other) == parent(source):
                join(source, other)
    groups = defaultdict(list)
    for p in leader:
        groups[find(p)].append(p)
    loose = []
    for members in groups.values():
        members.sort()
        if any(by_path[m]["type"] in ("script", "wrapper") for m in members):
            units.append({"kind": "linked" if len(members) > 1 else "single", "root": common_root(members), "members": members})
        else:
            loose.extend(members)
    # Unlinked documents, data and samples stay with the one group whose code sits in their folder,
    # or below the group's top folder when that folder holds group code itself.
    for path in list(loose):
        homes = [u for u in units if u["kind"] == "linked" and any(
            parent(path) == parent(m) or (parent(m) == u["root"] != "." and under(path, u["root"])) for m in u["members"])]
        if len(homes) == 1 and by_path[path]["type"] not in ("binary", "unknown"):
            homes[0]["members"].append(path)
            loose.remove(path)
    units.sort(key=lambda u: (u["root"], u["members"][0]))
    for number, unit in enumerate(units, 1):
        unit["id"] = f"u{number:03d}"
        unit["members"].sort()
        code = [by_path[m] for m in unit["members"] if by_path[m]["type"] in ("script", "wrapper")]
        unit["duplicate_of"] = code[0]["duplicate_of"] if unit["kind"] == "single" and code[0]["duplicate_of"] else None
    return units, sorted(loose)


def ordered_code(unit, by_path):
    order = {"wrapper": 0, "tool": 1, "module": 2, "test": 3}
    code = [by_path[m] for m in unit["members"] if by_path[m]["type"] in ("script", "wrapper")]
    return sorted(code, key=lambda r: (order.get(r["type"] if r["type"] == "wrapper" else r["role"], 4), r["path"]))


def unit_message(unit, by_path, limit=8):
    code, root = ordered_code(unit, by_path), unit["root"]
    others = [by_path[m] for m in unit["members"] if by_path[m]["type"] not in ("script", "wrapper")]
    inside = (lambda p: p) if root == "." else (lambda p: p[len(root) + 1:])
    title = code[0]["path"] if unit["kind"] == "single" else root
    lines = [f"UNIT: {title} ({len(code)} code file{'s' if len(code) != 1 else ''}, {len(others)} other)", "FILES:"]
    for record in code[:limit]:
        role = "launcher" if record["type"] == "wrapper" else ROLE_WORDS.get(record["role"], "code")
        summary = (record["summary"] or "No description available.")[:240]
        uses = ", ".join(dict.fromkeys(d for d in record["dependencies"] if d))[:120]
        lines.append(f"- {inside(record['path'])} ({record['language']}, {role}): {summary}" + (f" Uses: {uses}." if uses else ""))
    if len(code) > limit:
        lines.append(f"- and {len(code) - limit} more code files: " + ", ".join(inside(r["path"]) for r in code[limit:limit + 12]))
    if others:
        lines.append("OTHER FILES: " + ", ".join(f"{inside(r['path'])} ({r['type']})" for r in others)[:500])
    return "\n".join(lines)


def topic_messages(unit, by_path, topics):
    listing = "\n".join(f"- {t['id']}: {t['description']}" for t in topics["topics"])
    return [{"role": "system", "content": TOPIC_PROMPT.replace("{topics}", listing)},
            {"role": "user", "content": unit_message(unit, by_path)}]


def topic_schema(topics):
    return {"type": "object", "additionalProperties": False, "required": ["reason", "topic", "folder_name"],
            "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": 200},
                           "topic": {"type": "string", "enum": [t["id"] for t in topics["topics"]]},
                           "folder_name": {"type": "string", "minLength": 1, "maxLength": 60}}}


def keyword_topic(unit, by_path, topics):
    """Program-only topic from import names, description words and file names; None when unclear."""
    words = Counter()
    for record in ordered_code(unit, by_path):
        for dependency in record["dependencies"]:
            words[re.split(r"[.\s/-]", dependency.lower(), 1)[0]] += 3
        for word in WORD.findall((record["summary"] or "").lower()):
            words[word] += 1
        for word in WORD.findall(slug(record["path"].replace("/", " ")).replace("-", " ")):
            words[word] += 1
    scores = {t["id"]: sum(n for word, n in words.items() if word in t["hints"]) for t in topics["topics"]}
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    (best, top), (_, second) = ranked[0], ranked[1]
    return (best if top >= 3 and top >= 1.5 * second else None), {k: v for k, v in ranked if v}


def ask_topic(client, tag, unit, by_path, topics):
    started = time.monotonic()
    response = client.ask(tag, topic_messages(unit, by_path, topics), topic_schema(topics))
    seconds = round(time.monotonic() - started, 1)
    try:
        answer = json.loads(response["message"]["content"])
        ids = [t["id"] for t in topics["topics"]]
        if answer.get("topic") not in ids or not isinstance(answer.get("reason"), str):
            raise ValueError("unexpected answer")
    except (KeyError, TypeError, ValueError):
        return {"error": "The model answer could not be read.", "seconds": seconds}
    name = str(answer.get("folder_name", "")).strip().lower()
    if not FOLDER_NAME.match(name) or name in ids or VERSION_WORDS.search(name):
        name = None
    return {"topic": answer["topic"], "reason": answer["reason"].strip()[:200], "folder_name": name, "seconds": seconds}


def choose_topic(ai, rule):
    if ai and ai.get("topic"):
        if rule == ai["topic"]:
            return ai["topic"], 0.9, "The AI choice matches the program's keyword check."
        if rule is None:
            return ai["topic"], 0.75, "AI choice; the keyword check found no clear topic."
        return ai["topic"], 0.55, f"AI choice; the keyword check suggested {rule}. Check this one."
    if rule:
        return rule, 0.6, "Chosen by the program's keyword check (no AI answer)."
    return "other", 0.3, "No clear topic was found."


def unit_name(unit, by_path, ai):
    if unit["kind"] != "single" and unit["root"] != ".":
        return slug(posixpath.basename(unit["root"]))
    main = ordered_code(unit, by_path)[0]["path"]
    if ai and ai.get("folder_name"):
        return ai["folder_name"]
    stem, folder = Path(main).stem, posixpath.basename(parent(main))
    return slug(folder) if GENERIC_STEM.match(stem) and folder and folder != "." else slug(stem)


def inside(unit, path):
    """Path inside the proposed folder: single files keep their name, groups keep their layout."""
    if unit["kind"] == "single":
        return posixpath.basename(path)
    return path if unit["root"] == "." else path[len(unit["root"]) + 1:]


def readme_for(unit, by_path, topic_line, original):
    lines = [f"# {unit['folder'].split('/')[-1]}", "", topic_line, "", f"Original location: `{original}`", "", "## Files", ""]
    for record in ordered_code(unit, by_path):
        role = "launcher" if record["type"] == "wrapper" else ROLE_WORDS.get(record["role"], "code")
        same = record["duplicate_of"]
        note = f" Same content as `{inside(unit, same)}`." if same in unit["members"] else ""
        lines.append(f"- `{inside(unit, record['path'])}` ({record['language']}, {role}): {record['summary'] or 'No AI description.'}{note}")
        usage = (record.get("usage") or [])[:1]
        if usage:
            lines.append(f"  - How to use: {usage[0]}")
        if record["dependencies"]:
            lines.append("  - Needs: " + ", ".join(record["dependencies"][:6]))
    checks = [item for m in unit["members"] for item in by_path[m].get("unknowns") or []][:6]
    if checks:
        lines += ["", "## Check before use", ""] + [f"- {item}" for item in checks]
    lines += ["", "Written by Code Steward from AI explanations of the code. Check important claims against the code.", ""]
    return "\n".join(lines)


def loose_target(record):
    """Area and path for a file outside every group; the nearest named folder keeps its origin visible."""
    name = posixpath.basename(record["path"])
    folders = [part for part in record["path"].split("/")[:-1] if part.lower() not in GENERIC_FOLDERS]
    context = f"{slug(folders[-1])}/{name}" if folders else name
    if record["type"] in ("unknown", "binary"):
        return ("needs-review" if record["type"] == "unknown" else "bin"), name
    if record["sensitive"]:
        return "private", context
    return {"sample": "samples", "documentation": "docs", "data": "data"}.get(record["type"], "needs-review"), context


def build_plan(units, loose, by_path, topics):
    actions, used = [], set()
    kept = {}
    for unit in units:
        if unit["duplicate_of"]:
            continue
        base = f"{unit['topic']}/{unit['name']}" if unit["topic_confidence"] >= 0.5 else f"needs-review/{unit['name']}"
        folder, n = base, 2
        while folder in used:
            folder, n = f"{base}-{n}", n + 1
        used.add(folder)
        unit["folder"] = folder
        root = unit["root"]
        for member in unit["members"]:
            target = f"{folder}/{inside(unit, member)}"
            kept[member] = target
            actions.append({"action": "copy", "from": member, "to": target, "unit": unit["id"]})
            if by_path[member]["sensitive"]:
                actions.append({"action": "keep-out-of-git", "path": target, "reason": by_path[member]["sensitive_reason"]})
        readme = "CODE_STEWARD_NOTES.md" if any(DOC_NAME.match(posixpath.basename(m)) and parent(m) == root for m in unit["members"]) else "README.md"
        description = next(t["description"] for t in topics["topics"] if t["id"] == unit["topic"])
        topic_line = f"Topic: {unit['topic']} ({description})" + (f" {unit['ai_reason']}" if unit.get("ai_reason") else "")
        original = root if unit["kind"] != "single" else unit["members"][0]
        actions.append({"action": "write", "to": f"{folder}/{readme}", "unit": unit["id"],
                        "content": readme_for(unit, by_path, topic_line, original)})
    for unit in units:
        if unit["duplicate_of"]:
            for member in unit["members"]:
                same = by_path[member]["duplicate_of"]
                actions.append({"action": "skip", "from": member, "same_as": same, "kept_at": kept.get(same),
                                "reason": "Exact duplicate; one copy is kept and this one is recorded as the same file."})
    for path in loose:
        record = by_path[path]
        if record["duplicate_of"] and record["duplicate_of"] in kept:
            actions.append({"action": "skip", "from": path, "same_as": record["duplicate_of"], "kept_at": kept[record["duplicate_of"]],
                            "reason": "Exact duplicate; one copy is kept and this one is recorded as the same file."})
            continue
        area, relative = loose_target(record)
        target, n = f"{area}/{relative}", 2
        while target in kept.values():
            stem, dot, ext = relative.rpartition(".")
            target, n = f"{area}/{stem or ext}-{n}{dot + ext if stem else ''}", n + 1
        kept[path] = target
        actions.append({"action": "copy", "from": path, "to": target, "unit": None})
        if record["sensitive"]:
            actions.append({"action": "keep-out-of-git", "path": target, "reason": record["sensitive_reason"]})
    return actions


def organize(root, cases, rows, client=None, tag=None, topics=None, limit=1000, progress=say):
    topics = topics or load_topics()
    by_case = {case["name"]: case for case in cases}
    paths = scan(root, limit) if len(cases) > 1 else [cases[0]["name"]]
    records, sources = [], {}
    for rel in paths:
        record, source = classify_file(root, rel, by_case.get(rel), rows.get(rel))
        row = rows.get(rel) or {}
        if record["explained"]:
            record["usage"] = row["analysis"].get("usage") or []
            record["unknowns"] = row["analysis"].get("unknowns") or []
        records.append(record)
        if source is not None:
            sources[rel] = source
    mark_duplicates(records)
    by_path = {r["path"]: r for r in records}
    files = set(by_path)
    links, packages = {}, set()
    for rel, source in sources.items():
        found, package = links_for(rel, source, Path(rel).suffix.lower(), (by_case.get(rel) or {}).get("facts") or {}, files)
        links[rel] = sorted(found)
        if package:
            packages.add(rel)
    for rel, targets in links.items():
        for target in targets:
            by_path[target].setdefault("used_by", []).append(rel)
    units, loose = build_units(records, links, packages)
    work = [u for u in units if not u["duplicate_of"]]
    if client is not None:
        progress(f"Choosing a topic for {len(work)} group{'s' if len(work) != 1 else ''} of files with {tag}...")
    for number, unit in enumerate(work, 1):
        rule, scores = keyword_topic(unit, by_path, topics)
        ai = ask_topic(client, tag, unit, by_path, topics) if client is not None else None
        topic, confidence, note = choose_topic(ai, rule)
        unit.update(topic=topic, topic_confidence=confidence, topic_note=note, rule_topic=rule, rule_scores=scores,
                    ai_topic=(ai or {}).get("topic"), ai_reason=(ai or {}).get("reason"), ai_error=(ai or {}).get("error"),
                    ai_seconds=(ai or {}).get("seconds"), ai_folder_name=(ai or {}).get("folder_name"))
        unit["name"] = unit_name(unit, by_path, ai)
        if client is not None:
            progress(f"  {number}/{len(work)}  {topic:<24} {unit['name']}  ({unit['ai_seconds']} s)")
    actions = build_plan(units, loose, by_path, topics)
    folders = {u["id"]: u.get("folder") for u in units}
    for record in records:
        record.pop("usage", None)
        record.pop("unknowns", None)
    return {"version": ORGANIZE_VERSION, "mode": "dry-run", "topics_version": topics["version"],
            "files": records, "units": units, "loose": loose, "actions": actions,
            "summary": {"files": len(records), "types": dict(Counter(r["type"] for r in records)),
                        "sensitive": sum(r["sensitive"] for r in records),
                        "duplicates": sum(bool(r["duplicate_of"]) for r in records),
                        "groups": len(work), "folders": len({f for f in folders.values() if f}),
                        "needs_review": sum(a["action"] == "copy" and a["to"].startswith("needs-review/") for a in actions)}}


def split_of(path, split):
    sealed = split.get("sealed", [])
    return "sealed" if any(path == p or (p.endswith("/") and path.startswith(p)) for p in sealed) else "tuning"


def score(plan, reference, split, part="tuning"):
    """Compare a plan with the reference answers for ai_lab/samples. Only files of one split count."""
    by_path = {r["path"]: r for r in plan["files"]}
    unit_of = {m: u for u in plan["units"] for m in u["members"]}
    labels = {p: v for p, v in reference["classification"]["files"].items() if split_of(p, split) == part}
    result = {"split": part, "missing": sorted(p for p in labels if p not in by_path), "mistakes": []}

    def tally(name, ok, total):
        result[name] = {"correct": ok, "total": total}
    type_ok = role_total = role_ok = 0
    for path, label in labels.items():
        record = by_path.get(path)
        if not record:
            continue
        if record["type"] == label["type"]:
            type_ok += 1
        else:
            result["mistakes"].append(f"type {path}: {record['type']} (expected {label['type']})")
        if "role" in label and label["type"] == "script":
            role_total += 1
            if record["role"] in label["role"]:
                role_ok += 1
            else:
                result["mistakes"].append(f"role {path}: {record['role']} (expected {'/'.join(label['role'])})")
    present = [p for p in labels if p in by_path]
    tally("types", type_ok, len(present))
    tally("roles", role_ok, role_total)
    expected = {p for p in present if labels[p].get("sensitive")}
    found = {p for p in present if by_path[p]["sensitive"]}
    result["sensitive"] = {"found": len(expected & found), "expected": len(expected), "extra": sorted(found - expected),
                           "missed": sorted(expected - found)}
    for key in ("topic", "ai_topic", "rule_topic"):
        ok = first = total = 0
        for path in present:
            label, unit = labels[path], unit_of.get(path)
            if "topic" not in label or not unit or unit.get("duplicate_of"):
                continue
            total += 1
            chosen = unit.get(key)
            ok += chosen in label["topic"]
            first += chosen == label["topic"][0]
            if key == "topic" and chosen not in label["topic"]:
                result["mistakes"].append(f"topic {path}: {chosen} (expected {'/'.join(label['topic'])})")
        result[key + "s"] = {"acceptable": ok, "best": first, "total": total}
    plan_ref = reference.get("plan", {})

    def expand(items):
        return [p for item in items for p in (sorted(q for q in by_path if q.startswith(item)) if item.endswith("/") else [item])
                if p in by_path and split_of(p, split) == part]
    together = [expand(group) for group in plan_ref.get("keep_together", [])]
    ok = [len({unit_of[p]["id"] if p in unit_of else p for p in group}) == 1 for group in together if group]
    tally("kept_together", sum(ok), len(ok))
    apart = expand(plan_ref.get("keep_apart", []))
    ids = [unit_of[p]["id"] for p in apart if p in unit_of]
    tally("kept_apart", len(set(ids)), len(ids))
    skips = {a["from"]: a["same_as"] for a in plan["actions"] if a["action"] == "skip"}
    pairs = [pair for pair in plan_ref.get("not_copied_twice", []) if all(p in by_path for p in pair)]
    tally("duplicates_skipped", sum(skips.get(b) == a for a, b in pairs), len(pairs))
    review = [p for p in plan_ref.get("needs_review", []) if p in by_path]
    targets = {a["from"]: a["to"] for a in plan["actions"] if a["action"] == "copy"}
    tally("needs_review", sum(targets.get(p, "").startswith("needs-review/") for p in review), len(review))
    return result


TYPE_LABELS = {"wrapper": "launchers", "script": "scripts", "binary": "compiled programs", "sample": "samples",
               "data": "data files", "documentation": "documents", "unknown": "unknown files"}
AREAS = {"docs": "Documents that belong to no group.", "data": "Data files that belong to no group.",
         "samples": "Example inputs and outputs.", "bin": "Compiled programs, kept as one canonical copy.",
         "private": "Sensitive files that belong to no group; never added to Git.",
         "needs-review": "Files whose type or topic is unclear."}


def score_lines(result):
    def ratio(key):
        item = result[key]
        return f"{item['correct']}/{item['total']}"
    sensitive, topics = result["sensitive"], result["topics"]
    lines = [f"Compared with the reference answers ({result['split']} files only):",
             f"  File types: {ratio('types')} correct; code roles: {ratio('roles')}",
             f"  Sensitive files found: {sensitive['found']}/{sensitive['expected']}"
             + (f"; also flagged: {', '.join(sensitive['extra'])}" if sensitive["extra"] else ""),
             f"  Folder topic acceptable: {topics['acceptable']}/{topics['total']} (best choice {topics['best']}); "
             f"AI alone {result['ai_topics']['acceptable']}/{result['ai_topics']['total']}; "
             f"keyword check alone {result['rule_topics']['acceptable']}/{result['rule_topics']['total']}",
             f"  Kept together: {ratio('kept_together')} groups; kept apart: {ratio('kept_apart')} files; "
             f"duplicates skipped: {ratio('duplicates_skipped')}; unknown sent to review: {ratio('needs_review')}"]
    if result["missing"]:
        lines.append(f"  Missing from the plan: {', '.join(result['missing'])}")
    lines += [f"  - {item}" for item in result["mistakes"][:20]]
    if len(result["mistakes"]) > 20:
        lines.append(f"  ... {len(result['mistakes']) - 20} more in score.json")
    return "\n".join(lines)


def write_page(path, plan, topics):
    e = lambda value: html.escape(str(value), quote=True)
    summary, units = plan["summary"], [u for u in plan["units"] if u.get("folder")]
    copies = defaultdict(list)
    for action in plan["actions"]:
        if action["action"] == "copy":
            copies[action["unit"]].append(action)
    skips = [a for a in plan["actions"] if a["action"] == "skip"]
    counts = ", ".join(f"{n} {TYPE_LABELS[t]}" for t, n in sorted(summary["types"].items(), key=lambda item: -item[1]))
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             '<title>File Organization Plan</title>',
             '<style>body{font:16px/1.6 system-ui,sans-serif;color:#24313c;background:#f3f5f7;margin:0}'
             'main{max-width:960px;margin:36px auto;padding:0 20px}h1{font-size:28px}h2{font-size:21px;margin-top:32px}'
             'h3{font-size:17px;margin:0 0 4px}article{background:white;padding:18px 22px;margin:14px 0;border:1px solid #dbe2e8;border-radius:12px}'
             '.muted{color:#617080;font-size:14px}.note{color:#965514;background:#fff5e6;padding:12px;border-radius:8px}'
             'table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:5px 8px;border-top:1px solid #edf0f3;vertical-align:top}'
             'code{font-size:13px;overflow-wrap:anywhere}details{margin-top:8px}summary{cursor:pointer;color:#375c78}'
             '.low{color:#965514;font-weight:600}p,td,h3{overflow-wrap:anywhere}</style>',
             '<main><h1>File Organization Plan</h1>',
             '<p class="note">Nothing was copied, moved or deleted. This page is a proposal for review.</p>',
             f'<p>{summary["files"]} files: {e(counts)}. {summary["sensitive"]} sensitive, {summary["duplicates"]} exact '
             f'duplicate{"s" if summary["duplicates"] != 1 else ""}. {summary["folders"]} proposed folders.</p>']
    if plan.get("model"):
        parts.append(f'<p class="muted">Topics chosen by {e(plan["model"])} and checked against program keyword hints.</p>')
    else:
        parts.append('<p class="muted">No AI was used for topics: the program chose them from keyword hints.</p>')
    checks = [f"{u['folder']}/: {u['topic_note']}" for u in units if u["topic_confidence"] < 0.7]
    checks += [f"{r['path']}: {r['sensitive_reason']} It stays out of Git." for r in plan["files"] if r["sensitive"]]
    checks += [f"{r['path']}: the file type ({r['type']}) is uncertain." for r in plan["files"] if r["confidence"] < 0.6]
    checks += [f"{a['from']}: same content as {a['same_as']}; not copied again." for a in skips]
    if checks:
        parts.append('<h2>Check these first</h2><article><ul>' + ''.join(f'<li>{e(item)}</li>' for item in checks) + '</ul></article>')
    parts.append('<h2>Proposed folders</h2>')
    for topic in topics["topics"]:
        members = [u for u in units if u["folder"].split("/")[0] == topic["id"]]
        if not members:
            continue
        parts.append(f'<h3 style="margin-top:22px">{e(topic["id"])}/</h3><p class="muted">{e(topic["description"])}</p>')
        for unit in sorted(members, key=lambda u: u["folder"]):
            confidence = unit["topic_confidence"]
            origin = unit["root"] if unit["kind"] != "single" else unit["members"][0]
            rows = ''.join(f'<tr><td><code>{e(a["from"])}</code></td><td><code>{e(a["to"])}</code></td></tr>' for a in copies[unit["id"]])
            parts += [f'<article><h3>{e(unit["folder"])}/</h3>',
                      f'<p>{e(unit.get("ai_reason") or unit["topic_note"])}</p>',
                      f'<p class="muted">From <code>{e(origin)}</code> · topic confidence '
                      f'<span class="{"low" if confidence < 0.7 else ""}">{confidence:.2f}</span> · {e(unit["topic_note"])}</p>',
                      f'<details><summary>{len(copies[unit["id"]])} file(s)</summary><table><tr><th>Now</th><th>Proposed</th></tr>{rows}</table></details></article>']
    loose = [a for a in copies[None]]
    for area, description in AREAS.items():
        items = [a for a in loose if a["to"].split("/")[0] == area] + \
                [{"from": u["members"][0], "to": u["folder"]} for u in units if area == "needs-review" and u["folder"].startswith("needs-review/")]
        if items:
            parts.append(f'<h3 style="margin-top:22px">{e(area)}/</h3><p class="muted">{e(description)}</p><article><table>'
                         + ''.join(f'<tr><td><code>{e(a["from"])}</code></td><td><code>{e(a["to"])}</code></td></tr>' for a in items)
                         + '</table></article>')
    parts.append(f'<h2>Every file</h2><details><summary>Show all {summary["files"]} files with type and reasons</summary><table>'
                 '<tr><th>File</th><th>Type</th><th>Language</th><th>Confidence</th></tr>')
    for record in plan["files"]:
        kind = record["type"] + (f" ({record['role']})" if record["role"] else "") + (" · sensitive" if record["sensitive"] else "")
        reasons = " ".join(record["reasons"] + ([record["sensitive_reason"]] if record["sensitive"] else []))
        parts.append(f'<tr><td><code>{e(record["path"])}</code><div class="muted">{e(reasons)}</div></td><td>{e(kind)}</td>'
                     f'<td>{e(record["language"] or "")}</td><td class="{"low" if record["confidence"] < 0.6 else ""}">{record["confidence"]:.2f}</td></tr>')
    parts.append('</table></details><p class="muted">Confidence is computed by the program from how many independent signals agree, '
                 'not reported by the model.</p></main></html>')
    Path(path).write_text("\n".join(parts) + "\n", encoding="utf-8")


def organize_run(folder, use_ai=True, device=None, check=False, progress=say):
    folder, metadata, cases, rows, root = load_run(folder)
    config = dict(metadata["config"])
    if device:
        config["device"] = device
    topics = load_topics()
    client = tag = None
    if use_ai and metadata.get("mode") == "demo":
        progress("This is a demo run, so topics come from the program's keyword hints only.")
        use_ai = False
    if use_ai:
        client, tag = OllamaClient(config), metadata["models"][0]
        client.prepare(tag)
    started = time.monotonic()
    plan = organize(root, cases, rows, client, tag, topics, config.get("max_files", 1000), progress)
    plan.update(run_id=metadata.get("run_id"), source_root=str(root), model=tag,
                device=config["device"] if use_ai else None, wall_ms=round((time.monotonic() - started) * 1000),
                topic_prompt_hash=hashlib.sha256(TOPIC_PROMPT.encode("utf-8")).hexdigest() if use_ai else None)
    out = folder / "organization"
    out.mkdir(exist_ok=True)
    save_json(out / "plan.json", plan)
    write_page(out / "plan.html", plan, topics)
    if check:
        samples = SAMPLES.resolve()
        if root.resolve() != samples:
            raise LabError("Reference answers exist only for ai_lab/samples.", "input_error")
        result = score(plan, read_json(ANSWER_KEYS / "organization_answers.json"),
                       read_json(ANSWER_KEYS / "tuning_vs_sealed.json"))
        save_json(out / "score.json", result)
        progress(score_lines(result))
    progress(f"Plan saved: {out / 'plan.html'} (nothing was copied or moved).")
    return plan
