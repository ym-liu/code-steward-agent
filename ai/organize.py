"""Propose an organization plan from file records. A plan is a proposal: nothing is copied, moved or deleted.

Files that use each other stay together. The local model picks one topic per group from a fixed list (program
keyword hints are the fallback and the agreement check), and the plan proposes folders, notes and actions.
"""
import hashlib
import html
import json
import posixpath
import re
import time
import ast
from collections import Counter, defaultdict
from pathlib import Path

from .common import SETTINGS, AIError, digest, read_json
from .explain.facts import comment_lines, parse_python
from .explain.files import EXTENSIONS

ORGANIZE_VERSION = "1"
TOPIC_PROMPT = (SETTINGS / "topic_prompt.txt").read_text(encoding="utf-8").strip()
TYPES = ("wrapper", "script", "binary", "sample", "data", "documentation", "unknown")
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
DATA_LINE = re.compile(r"^(?:https?://\S+|[\w.+-]+@[\w.-]+\.\w+|[-+]?\d[\d.,:;/\s-]*|\S+(?:\s*[,;\t]\s*\S+)+|[\w.~/\\-]+\.\w{1,5})$")
CODE_FILE = r"[\w.~/\\%${}()-]*[\w-]\.(?:py|js|mjs|cjs|ts|r|rb|pl|sh|ps1|bat|cmd|sql|jar|php)\b"
INTERPRETER_RUN = re.compile(r"(?:^|[\s\"'(\[,;&|=])(?:python3?|py|node|rscript|ruby|perl|bash|sh|zsh|pwsh|powershell|java|"
                             rf"php|psql|cmd)(?:\.exe)?\b[^|;\n]*?({CODE_FILE})", re.I)
DIRECT_RUN = re.compile(r"""(?:^|[;&|(]\s*|\b(?:exec|call|start|nohup|source)\s+|^\s*\.\s+)["']?((?:\./|\.\\|%~dp0|\$\{?\w+\}?[/\\]|\$\(dirname[^)]*\)[/\\]|bin[/\\])[\w./\\-]+)""", re.I)
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
        raise AIError("topics.json needs unique lowercase-hyphen ids, each with a description, and 'other' last.", "config_error")
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


def executable_lines(source, suffix):
    skipped = comment_lines(source, suffix)
    if suffix == ".py":
        tree, _ = parse_python(source)
        # Standalone strings document code; mentioning another filename is not a dependency.
        for node in ast.walk(tree) if tree is not None else []:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                skipped.update(range(node.lineno, node.end_lineno + 1))
    return [(n, line) for n, line in enumerate(source.splitlines(), 1)
            if line.strip() and n not in skipped and not line.startswith("#!")]


def short_dependency(text):
    return re.split(r"\s+\(|:|\s+-\s", str(text), 1)[0].strip()[:30]


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
                           "folder_name": {"type": "string", "minLength": 1, "maxLength": 60},
                           "additional_topics": {"type": "array", "maxItems": 2, "items": {
                               "type": "object", "additionalProperties": False,
                               "properties": {"topic": {"type": "string", "enum": [t["id"] for t in topics["topics"]]},
                                              "reason": {"type": "string", "minLength": 1, "maxLength": 120}},
                               "required": ["topic", "reason"]}}}}


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
    try:
        messages = topic_messages(unit, by_path, topics)
        messages[0]["content"] += ("\nIn the SAME answer optionally include additional_topics: at most two other topic IDs "
                                   "with short reasons, only when clearly useful. Otherwise return an empty array.")
        response = client.ask(tag, messages, topic_schema(topics), num_predict=384)
    except AIError as error:
        return {"error": f"No answer from the model ({error}).", "request_failed": True,
                "seconds": round(time.monotonic() - started, 1)}
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
    additional = answer.get("additional_topics", [])
    if not isinstance(additional, list):
        additional = []
    seen = {answer["topic"]}
    checked = []
    for item in additional[:2]:
        if (isinstance(item, dict) and isinstance(item.get("topic"), str) and item["topic"] in ids
                and item["topic"] not in seen and isinstance(item.get("reason"), str) and item["reason"].strip()):
            checked.append({"topic": item["topic"], "reason": item["reason"].strip()[:120]})
            seen.add(item["topic"])
    return {"topic": answer["topic"], "reason": answer["reason"].strip()[:200], "folder_name": name,
            "additional_topics": checked, "seconds": seconds}


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


def unused(target, used):
    """target, or target with -2, -3 ... before its extension, so that no two actions share a target.

    Names are compared without case: on macOS and Windows README.md and readme.md are the same file.
    """
    folder, name = posixpath.split(target)
    stem, dot, ext = name.rpartition(".")
    if not stem:  # no extension, or a name like .env
        stem, dot, ext = name, "", ""
    candidate, n = target, 2
    while candidate.lower() in used:
        candidate, n = posixpath.join(folder, f"{stem}-{n}{dot}{ext}"), n + 1
    used.add(candidate.lower())
    return candidate


def kept_folder(unit, placed, used):
    """The library folder an earlier run used for this group, when its copied members agree and it is free."""
    folders = set()
    for member in unit["members"]:
        if member in placed:
            suffix = "/" + inside(unit, member)
            if not placed[member].endswith(suffix):
                return None
            folders.add(placed[member][:-len(suffix)])
    folder = folders.pop() if len(folders) == 1 else None
    return folder if folder and folder.lower() not in used else None


def build_plan(units, loose, by_path, topics, taken=None, placed=None):
    """Copy, skip, keep-out-of-git and README actions. taken: target names already used by other roots.

    Every folder and file target is reserved in taken, so no two actions write the same place.
    placed: library targets of files an earlier run copied. Their group keeps that folder (even if the
    model now suggests another name), so a changed file updates its copy instead of being copied again.
    """
    actions, used, placed = [], taken if taken is not None else set(), placed or {}
    kept = {}
    for unit in units:
        if unit["duplicate_of"]:
            continue
        base = f"{unit['topic']}/{unit['name']}" if unit["topic_confidence"] >= 0.5 else f"needs-review/{unit['name']}"
        folder = kept_folder(unit, placed, used)
        unit["folder_kept"] = folder is not None
        if folder is None:
            folder, n = base, 2
            while folder.lower() in used:
                folder, n = f"{base}-{n}", n + 1
        used.add(folder.lower())
        unit["folder"] = folder
        root = unit["root"]
        for member in unit["members"]:
            target = f"{folder}/{inside(unit, member)}"
            kept[member] = target
            used.add(target.lower())
            actions.append({"action": "copy", "from": member, "to": target, "unit": unit["id"]})
            if by_path[member]["sensitive"]:
                actions.append({"action": "keep-out-of-git", "path": target, "reason": by_path[member]["sensitive_reason"]})
        # The group's own README stays as it is; the program's notes then get another name.
        readme = "CODE_STEWARD_NOTES.md" if any(DOC_NAME.match(posixpath.basename(m)) and parent(m) == root for m in unit["members"]) else "README.md"
        description = next(t["description"] for t in topics["topics"] if t["id"] == unit["topic"])
        topic_line = f"Topic: {unit['topic']} ({description})" + (f" {unit['ai_reason']}" if unit.get("ai_reason") else "")
        original = root if unit["kind"] != "single" else unit["members"][0]
        actions.append({"action": "write", "to": unused(f"{folder}/{readme}", used), "unit": unit["id"],
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
        previous = placed.get(path)
        if previous and previous.lower() not in used:
            target = previous  # copied by an earlier run: keep its place in the library
            used.add(target.lower())
        else:
            target = unused(f"{area}/{relative}", used)
        kept[path] = target
        actions.append({"action": "copy", "from": path, "to": target, "unit": None})
        if record["sensitive"]:
            actions.append({"action": "keep-out-of-git", "path": target, "reason": record["sensitive_reason"]})
    return actions


def topic_key(unit, by_path, topics, tag):
    """Keep the original primary-topic cache key; old answers need not be rerun for optional labels."""
    return digest(json.dumps([tag, topic_messages(unit, by_path, topics)], sort_keys=True))


def plan_for(records, sources, facts, client=None, tag=None, topics=None, progress=say, topic_cache=None, taken=None,
             placed=None):
    """Group the files, choose a topic per group and propose folders (used by OrganizationService).

    records: one dict per file with a path relative to the scanned folder (type, role, summary,
    dependencies, usage, unknowns, sensitive, duplicate_of, ...); sources and facts: text and program
    facts of the code files. topic_cache (get/put) keeps AI topics of unchanged groups; taken keeps
    target names unique across several folders; placed keeps files where an earlier run copied them.
    Nothing is copied or moved.
    """
    topics = topics or load_topics()
    by_path = {r["path"]: r for r in records}
    files = set(by_path)
    links, packages = {}, set()
    for rel, source in sources.items():
        found, package = links_for(rel, source, Path(rel).suffix.lower(), facts.get(rel) or {}, files)
        links[rel] = sorted(found)
        if package:
            packages.add(rel)
    for rel, targets in links.items():
        by_path[rel]["local_dependencies"] = sorted(targets)
        for target in targets:
            by_path[target].setdefault("used_by", []).append(rel)
    units, loose = build_units(records, links, packages)
    work = [u for u in units if not u["duplicate_of"]]
    if client is not None:
        progress(f"Choosing a topic for {len(work)} group{'s' if len(work) != 1 else ''} of files with {tag}...")
    asking = client is not None
    for number, unit in enumerate(work, 1):
        rule, scores = keyword_topic(unit, by_path, topics)
        ai, cached = None, False
        if asking:
            key = topic_key(unit, by_path, topics, tag) if topic_cache is not None else None
            ai = topic_cache.get(key) if key else None
            cached = ai is not None
            if ai is None:
                ai = ask_topic(client, tag, unit, by_path, topics)
                if ai.get("request_failed"):
                    # Ollama stopped answering: one failed request does not stop the plan, and no more are sent.
                    asking = False
                    progress(f"  {ai['error']} Keyword hints choose the remaining topics.")
                elif key and ai.get("topic"):
                    topic_cache.put(key, ai)
        topic, confidence, note = choose_topic(ai, rule)
        unit.update(topic=topic, topic_confidence=confidence, topic_note=note, rule_topic=rule, rule_scores=scores,
                    ai_topic=(ai or {}).get("topic"), ai_reason=(ai or {}).get("reason"), ai_error=(ai or {}).get("error"),
                    additional_topics=(ai or {}).get("additional_topics", []),
                    ai_seconds=(ai or {}).get("seconds"), ai_folder_name=(ai or {}).get("folder_name"))
        unit["name"] = unit_name(unit, by_path, ai)
        if client is not None:
            elapsed = "cached" if cached else f"{unit['ai_seconds']} s"
            progress(f"  {number}/{len(work)}  {topic:<24} {unit['name']}  ({elapsed})")
    actions = build_plan(units, loose, by_path, topics, taken, placed)
    folders = {u["id"]: u.get("folder") for u in units}
    # Keep explanation fields: cross-topic/project reuse assessment needs interface and I/O information.
    return {"version": ORGANIZE_VERSION, "mode": "dry-run", "topics_version": topics["version"],
            "files": records, "units": units, "loose": loose, "actions": actions,
            "summary": {"files": len(records), "types": dict(Counter(r["type"] for r in records)),
                        "sensitive": sum(r["sensitive"] for r in records),
                        "duplicates": sum(bool(r["duplicate_of"]) for r in records),
                        "groups": len(work), "folders": len({f for f in folders.values() if f}),
                        "needs_review": sum(a["action"] == "copy" and a["to"].startswith("needs-review/") for a in actions)}}


TYPE_LABELS = {"wrapper": "launchers", "script": "scripts", "binary": "compiled programs", "sample": "samples",
               "data": "data files", "documentation": "documents", "unknown": "unknown files"}
AREAS = {"docs": "Documents that belong to no group.", "data": "Data files that belong to no group.",
         "samples": "Example inputs and outputs.", "bin": "Compiled programs, kept as one canonical copy.",
         "private": "Sensitive files that belong to no group; never added to Git.",
         "needs-review": "Files whose type or topic is unclear."}


def page_html(plan, topics, build_status=None):
    """The readable plan page; all model text is escaped."""
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
             '<p class="note">This page is a proposal: building it copied, moved or deleted nothing. To copy the files into '
             'the library, send this plan\'s ID to <code>POST /organization/preflight</code> and then '
             '<code>POST /organization/execute</code>.</p>',
             f'<p class="muted">Plan ID: <code>{e(plan.get("plan_id", ""))}</code></p>',
             f'<p>{summary["files"]} files: {e(counts)}. {summary["sensitive"]} sensitive, {summary["duplicates"]} exact '
             f'duplicate{"s" if summary["duplicates"] != 1 else ""}. {summary["folders"]} proposed folders.</p>']
    if build_status and build_status.get("state") == "building":
        parts.insert(1, '<meta http-equiv="refresh" content="10">')
        banner = ('<article role="status"><h2>Building a new plan</h2><p>' +
                  e(build_status.get("progress") or "Starting checks...") +
                  '</p><p>The results below are the previous saved plan. This page refreshes automatically.</p></article>')
        index = next(n for n, part in enumerate(parts) if part.startswith('<main>'))
        parts.insert(index + 1, banner)
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
    reuse = plan.get("reuse")
    if reuse:
        if reuse.get("state") == "partial":
            parts.append('<article role="alert"><h2>Reuse analysis is incomplete</h2><p>The plan was saved, '
                         'but some files or project matches were not checked. Build the plan again to retry; '
                         'completed assessments are cached. See the reasons below.</p></article>')
        parts.append('<h2>Additional topic placements</h2><p>A file can belong to more than one topic. '
                     'Extra copies go under the additional topic folder. Continue with preflight and execute. '
                     'This does not split projects or connect code between projects.</p>')
        parts.append(f'<p>Code files screened: {reuse.get("considered_files", 0)}. '
                     f'Files with all candidate checks completed: {reuse.get("assessed_files", 0)}. '
                     f'Files with no additional topic candidates: {reuse.get("files_without_candidates", 0)}. '
                     f'Files excluded: {len(reuse.get("skipped_files", []))}. '
                     f'Cached assessments: {reuse.get("cache_hits", 0)}.</p>')
        if reuse.get("unsupported_matches"):
            parts.append(f'<p>{reuse["unsupported_matches"]} weak AI matches were excluded because no concrete '
                         'use was demonstrated.</p>')
        if reuse.get("skipped_files"):
            parts.append('<details><summary>Files excluded from reuse</summary>')
            for skipped in reuse["skipped_files"]:
                parts.append(f'<p class="note">{e(skipped["file"])}: {e(skipped["reason"])}</p>')
            parts.append('</details>')
        if reuse.get("runtime_exclusions"):
            parts.append(f'<details><summary>{len(reuse["runtime_exclusions"])} combinations excluded before AI '
                         'because they require a language or framework change</summary>')
            for excluded in reuse["runtime_exclusions"]:
                parts.append(f'<p>{e(excluded["file"])} → {e(excluded["target"])}: {e(excluded["reason"])}</p>')
            parts.append('</details>')
        for error in reuse.get("errors", []):
            parts.append(f'<p class="note">{e(error["source"])}: {e(error["reason"])}</p>')
        for suggestion in reuse.get("suggestions", []):
            payload = json.dumps({"plan_id": plan.get("plan_id"), "suggestion_id": suggestion["id"],
                                  "accept": suggestion["state"] != "accepted"}, indent=2)
            destination = next((u["folder"] for u in units if u["id"] == suggestion["target"]), suggestion["target"])
            parts.append(f'<article><h3>{e(suggestion.get("source_file", suggestion["source"]))} → {e(destination)} '
                         f'({e(suggestion["kind"])})</h3><p>{e(suggestion["reason"])}</p>'
                         f'<p>Assessment: {e(suggestion["verdict"])} · Review: {e(suggestion["state"])}</p>')
            for label, key in (() if suggestion["kind"] == "topic" else (("Integration requirements", "requirements"), ("Unknowns", "unknowns"))):
                parts.append(f'<p>{label}: {e("; ".join(suggestion[key]) or "None reported by the model.")}</p>')
            parts.append('<ul>' + ''.join(f'<li>{e(c.get("evidence_id", ""))} {e(c["group"])} / {e(c["file"])}: {e(c["quote"])}</li>'
                                         for c in suggestion["evidence"]) + '</ul>')
            parts.append('<details><summary>Proposed additional copies</summary><ul>' +
                         ''.join(f'<li><code>{e(path)}</code></li>' for path in suggestion.get("destinations", [])) +
                         '</ul></details>')
            parts.append('<details><summary>Optional override</summary><p>Send to '
                         '<code>POST /organization/reuse/review</code> to exclude an included copy or include a '
                         'suggestion left out. This is optional. It returns a new plan ID.</p><pre>' + e(payload) +
                         '</pre></details></article>')
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
                      f'<span class="{"low" if confidence < 0.7 else ""}">{confidence:.2f}</span> · {e(unit["topic_note"])}'
                      + (' · kept in the folder an earlier run used' if unit.get("folder_kept") else '') + '</p>',
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
    return "\n".join(parts) + "\n"


