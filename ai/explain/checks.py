"""Program-side checks of explanation claims against the source and static facts.

These are cheap heuristics, not proof of correctness: a flagged item needs a person or a
repair request; an unflagged answer can still be wrong.
"""
import ast
from pathlib import PurePosixPath
import re

from .answer import SCHEMA
from .facts import (ALL_ARGUMENTS, CPP_HEADERS, FILE_EXTENSIONS, JAVA_UTIL, NOT_FUNCTIONS, SCRIPT_EXTENSIONS,
                    comment_lines, parse_python, passed_by_script)

CHECKS_VERSION = "6"
LIST_FIELDS = ("inputs", "outputs", "usage", "dependencies", "relationships", "unknowns")
PLACEHOLDERS = {"none", "null", "n/a", "na", "nothing", "not applicable", "none explicitly referenced",
                "no dependencies", "no relationships", "no inputs", "no outputs", "unknown"}
LANGUAGE_NAMES = {"python", "python 3", "python3", "python standard library", "python interpreter", "java",
                  "java runtime", "jdk", "jre", "java standard library", "javascript", "typescript", "node",
                  "c", "c++", "c++ standard library", "c++ compiler", "c#", "go", "golang", "rust", "ruby",
                  "perl", "php", "r", "lua", "swift", "kotlin", "sql", "bash", "shell", "sh", "standard library"}
BUILTIN_TYPE = re.compile(r"^(list|dict|set|tuple|int|str|float|bool|string|integer|array)(\[.*\])?$", re.I)
STANDARD_TYPES = JAVA_UTIL | set(CPP_HEADERS) | {"String", "Integer", "Object", "List", "Dict", "Optional",
                                                  "deque", "defaultdict", "Counter", "OrderedDict"}
# Install names that differ from the import name are correct dependency names.
INSTALL_NAMES = {"opencv-python": "cv2", "opencv-python-headless": "cv2", "opencv": "cv2", "scikit-learn": "sklearn",
                 "scikit-image": "skimage", "pillow": "PIL", "pyyaml": "yaml", "beautifulsoup4": "bs4",
                 "python-dateutil": "dateutil", "pytorch": "torch", "tensorflow-hub": "tensorflow_hub",
                 "python-dotenv": "dotenv", "pymupdf": "fitz", "attrs": "attr", "faiss-cpu": "faiss",
                 "google-colab": "google.colab", "protobuf": "google.protobuf", "pywin32": "win32api"}
# Well-known libraries and services; naming one the code never uses is an invented requirement.
KNOWN_LIBRARIES = {"pytorch": "torch", "torch": "torch", "tensorflow": "tensorflow", "keras": "keras",
                   "numpy": "numpy", "pandas": "pandas", "scikit-learn": "sklearn", "sklearn": "sklearn",
                   "opencv": "cv2", "django": "django", "flask": "flask", "fastapi": "fastapi", "ollama": "ollama",
                   "matplotlib": "matplotlib", "jupyter": "jupyter", "docker": "docker", "kubernetes": "kubernetes"}
COMMON_BUILTINS = {"range", "len", "print", "enumerate", "zip", "sorted", "sum", "min", "max", "abs", "isinstance",
                   "open", "int", "str", "float", "dict", "list", "set", "tuple", "map", "filter", "any", "all",
                   "reversed", "round"}
RUN_COMMAND = re.compile(r"\b(?:python3?|py|java|javac|node|deno|ruby|perl|php|go run|cargo run|g\+\+|gcc|clang)\s+"
                         r"(?:-\S+\s+)*[\w./\\-]+\.(?:py|java|js|ts|rb|pl|php|go|rs|cpp|cc|c)\b|\bpython3?\s+-m\s+\S"
                         r"|(?-i:\bjava\s+[A-Z]\w*)|(?:^|\s)\./[\w.-]+", re.I)  # "java Solution", not "Java Runtime"
RUN_PHRASE = re.compile(
    r"\b(?:run|execute|launch|start)\s+(?:the|this|it)\s*(?:script|program|file|code)?\s*(?:directly|with|from)"
    r"|\bruns?\s+(?:it\s+|this\s+(?:file|script)\s+)?directly\b"
    r"|\bexecut(?:e|es|ed|ing)\s+(?:it\s+|this\s+(?:file|script)\s+)?(?:directly|as an? (?:script|program))"
    r"|\brun\s+(?:it\s+)?(?:as an? (?:script|program|module)|from the command line|in an IDE)"
    r"|\bwhen (?:it is )?executed as a script\b|\bstarts automatically\b"
    r"|\b(?:compile and run|standalone (?:script|program)|top-level script|main entry point)\b", re.I)
# Negation is judged per clause: "No arguments; run it directly" still claims it runs.
NEGATION = re.compile(r"\b(?:not|cannot|can't|never|isn't|doesn't|don't|nothing|neither|nor)\b"
                      r"|\bno (?:direct|entry|main|run)\b|\bno\b[^;.]{0,60}\bto (?:run|execute|start)\b", re.I)
CLAUSE = re.compile(r"[;.()]|\bbut\b|\bthough\b|\bhowever\b", re.I)
FLAG = re.compile(r"(?<![\w-])--[A-Za-z][\w-]*")
FILE_NAME = re.compile(rf"[\w./\\-]*\w\.(?:{FILE_EXTENSIONS}|h|hpp|cpp|java|go|rs|rb)\b", re.I)
WORD = re.compile(r"[A-Za-z_][\w.+#-]*")
NON_ENGLISH = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0400-\u04ff]")
INTERNAL = re.compile(r"\bFACTS\b|\b(?:runs_when_executed|entry_point|names_without_visible_definition|"
                      r"file_references|reads_files|writes_files|runs_commands|argument_declarations|command_line|"
                      r"runtime_note|parse_error\w*|asks_user_for_input|main_imports|unused_imports|"
                      r"definitions_total|line_count|source_dialect|shell_commands)\b")
SPECULATION = re.compile(r"\b(?:likely|probably|possibly|appears to|seems to|might be|may be|suggests)\b", re.I)
CLAIMS_MISSING = re.compile(r"\b(?:not|never)\s+(?:been\s+)?(?:imported|defined|declared|included)\b|\bmissing\b"
                            r"|\bundefined\b|\bno (?:import|definition)\b|\bwithout (?:an? )?(?:import|definition)\b", re.I)
INPUT_FILLER = re.compile(r"\b(?:calculated|computed|used|set|created) internally\b|\binternal (?:variable|state|counter)s?\b"
                          r"|\bloop (?:index|indices|variable|counter)s?\b"
                          r"|\b(?:row|column)(?: and (?:row|column))? ind(?:ex|ices)\b", re.I)
INPUT_NOTES = re.compile(r"\bcalled by other code\b|\bnot (?:executed|run) directly\b|\bno command-line\b"
                         r"|\bno arguments? (?:are|is) (?:required|needed)\b", re.I)
OUTPUT_FILLER = re.compile(r"\bif any\b|\bnone (?:visible|shown)\b|\binternal variable\b|\bnot (?:a )?visible\b"
                           r"|\binto memory\b|\b(?:via|through) inheritance\b|\bis called by other code\b|^\s*no\b", re.I)
UNKNOWN_FILLER = re.compile(r"\b(?:is|are) not (?:required|needed)\b"
                            r"|\bno (?:external )?(?:libraries|packages|dependencies) (?:are |is )?(?:required|needed)\b", re.I)
CODE_CALL = re.compile(r"\b([A-Za-z_]\w*)\(")
CREATED_OBJECT = re.compile(r"\b(?:create|instantiate|construct)\s+(?:an?\s+|the\s+)?([A-Z]\w*)\b(?:\s+object)?")
SIGNATURE = re.compile(r"^(\w+)\((.*)\)(?: -> (.+))?$")
ANY_FUNCTION = re.compile(r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|virtual|inline|"
                          r"explicit|async|override)\s+)*[\w:<>,\[\]?*&.]+(?:<[^()]*>)?[\s*&]+(\w+)\s*\([^;{}]*\)\s*"
                          r"(?:const\s*)?(?:override\s*)?(?:throws\s+[\w., ]+)?\{?\s*$")
# Everyday words that are also common variable names; they only count as code names when written like code.
COMMON_WORDS = {"total", "sum", "list", "map", "set", "file", "files", "data", "result", "results", "value", "values",
                "key", "keys", "node", "nodes", "input", "inputs", "output", "outputs", "count", "index", "item",
                "items", "name", "names", "path", "text", "line", "lines", "size", "type", "types", "time", "date",
                "main", "run", "test", "tests", "state", "config", "settings", "model", "models", "case", "cases",
                "source", "target", "word", "words", "grid", "board", "root", "left", "right", "head", "tail", "next",
                "prev", "stack", "queue", "cache", "table", "row", "rows", "col", "cols", "string", "number", "numbers"}
# Words that describe a dependency rather than name it.
FILLER = {"for", "from", "the", "a", "an", "and", "or", "of", "to", "in", "on", "by", "with", "via", "as", "is",
          "module", "modules", "library", "libraries", "package", "packages", "class", "classes", "type", "types",
          "hint", "hints", "implied", "specifically", "used", "uses", "use", "using", "standard", "builtin",
          "built-in", "function", "functions", "main", "runtime", "environment", "api", "service", "tool",
          "program", "file", "files", "config", "configuration", "required", "optional", "defined", "data",
          "e.g", "etc", "external", "support", "parsing", "reading", "writing", "handling", "operations"}


def normalize(text):
    return re.sub(r"\s*\([^)]*\)", "", text).strip().strip(".:;,'\"`").lower()


def mentioned(token, source):
    """Whole-word match; dotted names also match their first or last part (java.util.HashMap)."""
    parts = {token, *([token.split(".")[0], token.split(".")[-1]] if "." in token.strip(".") else [])}
    return any(part and re.search(r"(?<!\w)" + re.escape(part) + r"(?!\w)", source, re.I) for part in parts)


def named_in(text, source):
    """True when a meaningful word of text appears in the code (loosely: "sstream" in ostringstream)."""
    words = [w.strip(".") for w in WORD.findall(text) if w.strip(".").lower() not in FILLER]
    return not words or any(mentioned(w, source) or len(w) >= 5 and w.lower() in source.lower()
                            or w.lower() in INSTALL_NAMES and mentioned(INSTALL_NAMES[w.lower()], source) for w in words)


def names_in_file(case):
    """(classes/functions defined anywhere, names bound by imports, Python variables that stay internal)."""
    facts = case["facts"]
    defined = {item["name"] for item in facts.get("definitions", [])}
    imported = {name.split(".")[-1] for name in facts.get("imports", []) if not name.startswith(".")}
    assigned, visible = set(), set()
    if facts.get("extension") not in (".py", ".txt"):
        skipped = comment_lines(case["source"], facts.get("extension"))
        for number, line in enumerate(case["source"].splitlines(), 1):
            found = None if number in skipped else ANY_FUNCTION.match(line)
            if found and found.group(1) not in NOT_FUNCTIONS:
                defined.add(found.group(1))
    if facts.get("extension") == ".py":
        tree, _ = parse_python(case["source"])
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(node.name)
                elif isinstance(node, ast.alias) and node.name != "*":
                    imported.add(node.asname or node.name.split(".")[0])
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    assigned.add(node.id)
                elif isinstance(node, ast.arg):
                    visible.add(node.arg)
                elif isinstance(node, (ast.Return, ast.Yield)) and node.value is not None:
                    visible.update(child.id for child in ast.walk(node.value) if isinstance(child, ast.Name))
    return defined, imported, assigned - visible - defined


ASSIGNMENT = re.compile(r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)?|const\s+|let\s+|var\s+|final\s+)?"
                        r"\$?([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")


def assigned_names(case):
    """Variable names given a value anywhere in the file (a claim that one is missing is then wrong)."""
    names = set()
    if case["facts"].get("extension") == ".py":
        tree, _ = parse_python(case["source"])
        if tree is not None:
            return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    for line in case["source"].splitlines():
        found = ASSIGNMENT.match(line)
        if found:
            names.add(found.group(1))
    return names


def clean_answer(analysis):
    """Drop placeholder and duplicate items in place; asking the model for this lost good items."""
    removed = []
    # Spaces or line breaks around an item are layout, not content.
    if isinstance(analysis.get("purpose"), str):
        analysis["purpose"] = analysis["purpose"].strip() or None
    for field in LIST_FIELDS:
        analysis[field] = [item.strip() for item in analysis.get(field, []) if item.strip()]
    if isinstance(analysis.get("purpose"), str) and normalize(analysis["purpose"]) in PLACEHOLDERS:
        removed.append({"field": "purpose", "item": analysis["purpose"]})
        analysis["purpose"] = None
    for field in LIST_FIELDS:
        kept = []
        for item in analysis.get(field, []):
            if normalize(item) in PLACEHOLDERS or item in kept:
                removed.append({"field": field, "item": item})
            else:
                kept.append(item)
        analysis[field] = kept
    return removed


def split_parameters(text):
    """Split "Map<A, B> m, int k" at top-level commas only."""
    parts, depth, current = [], 0, ""
    for char in text:
        depth += char in "<([" and 1 or char in ">)]" and -1 or 0
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    return [part for part in parts + [current.strip()] if part]


def public_calls(facts, source):
    """(class or None, name, parameter text, return type) for the functions and methods other code would use.

    Class methods are the class's interface. Module functions and methods that the file never calls itself
    come before helpers, because they are usually what other code calls (Solution.findWords before the
    TrieNode.addWord it uses).
    """
    methods, functions = [], []
    for item in facts.get("definitions", []):
        for text in item.get("methods", []):
            found = SIGNATURE.match(text)
            if found:
                methods.append((item["name"], *found.groups()))
        if "parameters" in item and not item["name"].startswith("_"):
            functions.append((None, item["name"], ", ".join(item["parameters"]), item.get("returns")))
        elif "signature" in item and SIGNATURE.match(item["signature"]):
            functions.append((None, *SIGNATURE.match(item["signature"]).groups()))
    used_inside = lambda call: len(re.findall(rf"\b{re.escape(call[1])}\s*\(", source)) > 1
    outside = [call for call in functions if not used_inside(call)]
    entry_methods = [call for call in methods if call[0] != call[1] and not used_inside(call)]
    return list(dict.fromkeys(outside or (entry_methods + methods) or functions))


def fill_from_facts(analysis, case):
    """Fill empty inputs, outputs or usage of an explained file from the program's facts, in place."""
    facts = case["facts"]
    if analysis.get("purpose") is None:
        return []
    library = "entry_point" in facts and facts["entry_point"] is None
    calls = public_calls(facts, case["source"]) if library else []
    names = lambda text: ", ".join(p.split()[-1].lstrip("*&") for p in split_parameters(text))
    found = {}
    if not analysis["inputs"]:
        declared = [name for item in facts.get("argument_declarations", []) for name in item["names"]]
        found["inputs"] = [", ".join(declared) + " (command-line arguments)"] if declared else \
            list(dict.fromkeys(f"{params} (passed to {name})" for owner, name, params, _ in calls if params))[:3]
    if not analysis["outputs"]:
        found["outputs"] = list(dict.fromkeys(f"{result} returned by {name}" for owner, name, _, result in calls
                                              if result and result not in ("None", "void") and name != owner))[:3]
    if not analysis["usage"] and calls:
        owner, name, params, _ = next((call for call in calls if call[1] != call[0]), calls[0])
        after = f", after the syntax error at line {facts['parse_error_line']} is fixed" if facts.get("parse_error_line") else ""
        found["usage"] = [f"From other code, create a {owner} object and call {name}({names(params)}){after}." if owner
                          else f"From other code, call {name}({names(params)}){after}."]
    filled = []
    for field, items in found.items():
        if items:
            analysis[field] = items
            filled += [{"field": field, "item": item} for item in items]
    return filled


def claims_to_run(item):
    if RUN_COMMAND.search(item):
        return True
    return any(RUN_PHRASE.search(clause) and not NEGATION.search(clause) for clause in CLAUSE.split(item))


def check_claims(analysis, case):
    """Return [{field, item, problem[, minor]}] for claims that conflict with the code or facts."""
    facts, source = case["facts"], case["source"]
    own = PurePosixPath(case["name"]).name
    lines = source.splitlines()
    defined, imported, internal = names_in_file(case)
    defined_lower = {name.lower() for name in defined}
    imported_lower = {name.lower() for name in imported} | {name.lower() for name in facts.get("imports", [])
                                                           if not name.startswith(".")}
    missing = {item["name"] for item in facts.get("names_without_visible_definition", [])}
    unknown_language = facts.get("language", "unknown").startswith("unknown")
    purpose = analysis.get("purpose")
    issues = []

    def flag(field, item, problem, minor=False):
        issue = {"field": field, "item": item, "problem": problem, **({"minor": True} if minor else {})}
        if issue not in issues:
            issues.append(issue)

    if purpose is None and not unknown_language:
        flag("purpose", None, "Describe what the readable code does; a syntax error or a comment addressed to you "
                              "does not make the purpose unknown. Put any error in unknowns.")
    if purpose is None and unknown_language:
        for item in analysis.get("usage", []):
            flag("usage", item, "The purpose is unknown, so leave usage empty.")
    elif purpose and unknown_language and (SPECULATION.search(purpose) or re.search(r"\bunknown\b", purpose, re.I)):
        flag("purpose", purpose, "The syntax is not recognized, so do not guess what it is for; "
                                 "use null and explain the gap in unknowns.")
    texts = [("purpose", purpose or "")] + [(field, item) for field in LIST_FIELDS for item in analysis.get(field, [])] \
        + [("evidence", item.get("claim", "")) for item in analysis.get("evidence", [])]
    import_names = {name.split(".")[0].lower() for name in facts.get("imports", []) if not name.startswith(".")}
    for field, text in texts:
        # Seen in one-line answers during speed tuning; also real problems in normal answers.
        if "\n" in text.strip():
            flag(field, text, "Put one thing per item without line breaks; group related names in one short item, "
                              'for example "a, b: what they are".')
        limit = (SCHEMA["properties"]["purpose"] if field == "purpose" else SCHEMA["properties"]["inputs"]["items"]
                 if field != "evidence" else SCHEMA["properties"]["evidence"]["items"]["properties"]["claim"])["maxLength"]
        if len(text) >= limit:
            flag(field, text, "This text reached the length limit and was cut off; write it shorter.")
        if field == "usage" and len({word.lower() for word in re.findall(r"[\w.-]+", text)} & import_names) >= 2:
            flag(field, text, "Do not list libraries in usage; they belong in dependencies. Say only how to start or call it.")
        if NON_ENGLISH.search(text):
            flag(field, text, "Write every field in English.")
        if INTERNAL.search(text):
            flag(field, text, "Describe the code in plain words; do not mention the program's facts or their field names.")
    for field in LIST_FIELDS:
        for item in analysis.get(field, []):
            if normalize(item) in PLACEHOLDERS:
                flag(field, item, 'Use [] instead of a placeholder item such as "None".')
            if field in ("inputs", "dependencies", "relationships") and own.lower() in item.lower():
                flag(field, item, f"{own} is this file itself; list only other files, programs or configs.")
            if field in ("inputs", "outputs", "dependencies", "relationships"):
                for name in FILE_NAME.findall(item):
                    base = re.split(r"[\\/]", name)[-1]
                    if base.lower() != own.lower() and not mentioned(base, source):
                        flag(field, item, f'"{base}" is not named anywhere in the code; list only files the code names.')
            if field in ("usage", "inputs"):
                for option in FLAG.findall(item):
                    if not mentioned(option, source):
                        flag(field, item, f"{option} does not appear in the code; never invent command-line options.")
    for item in analysis.get("inputs", []):
        if INPUT_FILLER.search(item):
            flag("inputs", item, "This is worked out inside the code, not supplied by a person or caller; remove it.")
        elif INPUT_NOTES.search(item):
            flag("inputs", item, "This is a note about how the file is used, not something a person or caller "
                                 "supplies; remove it.")
    for item in analysis.get("outputs", []):
        if OUTPUT_FILLER.search(item):
            flag("outputs", item, "This is not a visible result of running or calling the code; remove it.")
    for field in ("inputs", "outputs"):
        for item in analysis.get(field, []):
            names = re.match(r"\s*`?([A-Za-z_]\w*)`?(?:\s*,\s*`?([A-Za-z_]\w*)`?)?\s*[:(]", item)
            listed = {name for name in (names.groups() if names else ()) if name} | \
                {token for token in re.findall(r"[A-Za-z_]\w*", item) if re.search(r"[a-z][A-Z]|_", token)}
            if listed & internal and not any(issue["field"] == field and issue["item"] == item for issue in issues):
                flag(field, item, "This names a variable used only inside the code; describe what a person or "
                                  "caller supplies or receives instead.")
    for field in ("usage", "dependencies", "relationships"):
        for item in analysis.get(field, []):
            for word in re.findall(r"[A-Za-z][\w.+-]*", item):
                module = KNOWN_LIBRARIES.get(word.lower())
                if module and not mentioned(word, source) and not mentioned(module, source):
                    flag(field, item, f"{word} is not used by this code; mention only what the code imports or runs.")
    for item in analysis.get("dependencies", []):
        core = normalize(item)
        if core in PLACEHOLDERS:
            continue
        # "Python standard library (sys, pathlib)" names real modules in its parentheses.
        specific = [w for w in WORD.findall(" ".join(re.findall(r"\(([^)]*)\)", item)))
                    if w.lower() not in FILLER and mentioned(w, source)]
        if (core in LANGUAGE_NAMES or BUILTIN_TYPE.match(core) and "typing" not in item.lower()) and not specific:
            flag("dependencies", item, "The language or a built-in type is not a dependency; name the specific "
                                       "libraries, programs or configs used, or leave it out.")
        elif re.sub(r"\b(class|struct|interface|enum)\b", "", core).strip() in defined_lower:
            flag("dependencies", item, "This is defined in the file itself, so it is not a dependency.")
        elif not named_in(item, source):
            words = [w for w in WORD.findall(item) if w.lower() not in FILLER]
            flag("dependencies", item, f'"{words[0]}" does not appear in the code; list only what the code actually uses.')
    for item in analysis.get("relationships", []):
        head = re.split(r":\s|\s[-\u2013]\s|\s\(", item, maxsplit=1)[0].strip().strip("`'\"")
        core = re.sub(r"\b(?:the|class|struct|interface|enum|type|module|library|package)\b", "", head, flags=re.I).strip()
        if not core or core.startswith(".") or own.lower() in item.lower() or normalize(item) in PLACEHOLDERS:
            continue  # relative imports are real relationships; self-references are flagged above
        if core.lower() in defined_lower or core in missing or core.lower() in imported_lower \
                or core in STANDARD_TYPES or BUILTIN_TYPE.match(core):
            flag("relationships", item, f"{core} is a class, type or library, not a related file, program or config; "
                                        "mention it under dependencies or unknowns instead.")
        elif not named_in(core, source):
            flag("relationships", item, f'"{core}" is not named in the code; list only files, programs or configs the code names.')
    code_like = lambda token, item: token[0].isupper() or "_" in token or re.search(
        rf"[`'\"]{re.escape(token)}[`'\"]|\b{re.escape(token)}\s*\(|^{re.escape(token)}\s*:", item)
    for item in analysis.get("unknowns", []):
        if len(item) >= 20 and item.strip().rstrip(".") in source:
            flag("unknowns", item, "This text is copied from the code; describe a real gap or leave unknowns empty.")
        if UNKNOWN_FILLER.search(item):
            flag("unknowns", item, "This is not a gap that stops the reader; remove it.")
        if re.search(r"\bbuilt[- ]?in\b", item, re.I):
            for name in sorted(name for name in missing if re.search(rf"\b{re.escape(name)}\b", item)):
                flag("unknowns", item, f"{name} is not built in; it has to be imported or defined before use.")
        if CLAIMS_MISSING.search(item):
            assigned = assigned_names(case)
            for token in dict.fromkeys(re.findall(r"[A-Za-z_]\w*", item)):
                if len(token) >= 3 and token not in missing and (token in defined or token in imported) \
                        and (code_like(token, item) or token.lower() not in COMMON_WORDS):
                    flag("unknowns", item, f"{token} is defined or imported in this file, so it is not missing.")
                elif len(token) >= 3 and token not in missing and token in assigned and code_like(token, item):
                    flag("unknowns", item, f"{token} is given a value in this file, so it is not missing.")
                elif facts.get("extension") == ".py" and token in COMMON_BUILTINS and re.search(r"\bimport", item, re.I) \
                        and re.search(rf"\b{token}\s*\(", source):
                    flag("unknowns", item, f"{token} is built into Python and needs no import.")
    for item in analysis.get("usage", []):
        for name in dict.fromkeys(CODE_CALL.findall(item) + CREATED_OBJECT.findall(item)):
            if name.lower() not in ("python", "python3", "java", "node") and not mentioned(name, source):
                flag("usage", item, f"{name} is not in this code; describe how to use the classes and functions "
                                    "this file defines.")
    if "entry_point" in facts and facts["entry_point"] is None:
        for item in analysis.get("usage", []):
            if claims_to_run(item):
                flag("usage", item, "Nothing runs when this file is executed directly (no main guard, main function "
                                    "or top-level code). Describe how other code imports and calls it instead.")
    if facts.get("extension") in SCRIPT_EXTENSIONS:
        fixed = {token for line in lines if ALL_ARGUMENTS.search(line) for token in passed_by_script(line)[1]}
        for item in analysis.get("usage", []):
            for option in sorted(token for token in fixed if token.startswith("-")):
                if own in item and option in item:
                    flag("usage", item, f"The script already passes {option} itself; the user only adds the "
                                        "arguments that it forwards.")
        for path in sorted(token for token in fixed if not token.startswith("-")):
            stem = re.split(r"[\\/]", path)[-1].split(".")[0]
            for item in analysis.get("usage", []):
                if stem and re.search(rf"\b(?:followed by|with|pass(?:ing)?|provide|give)\s+(?:the\s+|a\s+)?"
                                      rf"(?:path to the\s+)?{re.escape(stem)}\b", item, re.I):
                    flag("usage", item, f"The script passes {path} itself; the user only adds the arguments that it forwards.")
            for item in analysis.get("inputs", []):
                if stem and re.search(rf"\b{re.escape(stem)}\b", item, re.I):
                    flag("inputs", item, f"The script passes {path} itself, so the user does not supply it; "
                                         "list it under relationships instead.")
    # Omissions are reported but do not cost a repair request.
    definitions = facts.get("definitions", [])
    signatures = [method for item in definitions for method in item.get("methods", [])] \
        + [item["signature"] for item in definitions if "signature" in item]
    takes_input = facts.get("argument_declarations") or facts.get("command_line") or facts.get("reads_files") \
        or any(item.get("parameters") for item in definitions) or any(not s.split("(", 1)[1].startswith(")") for s in signatures)
    gives_output = facts.get("writes_files") or any(item.get("returns") not in (None, "None") for item in definitions) \
        or any(" -> " in s and not s.endswith("-> None") for s in signatures)
    if purpose and takes_input and not analysis.get("inputs"):
        flag("inputs", None, "Inputs are empty, but the code takes arguments, parameters or files.", minor=True)
    if purpose and gives_output and not analysis.get("outputs"):
        flag("outputs", None, "Outputs are empty, but the code returns values or writes files.", minor=True)
    comments = comment_lines(source, facts.get("extension"))
    for item in analysis.get("evidence", []):
        number = item.get("line")
        if number in comments:
            flag("evidence", f"line {number}", f"Line {number} is a comment; cite an executable statement instead.", minor=True)
    return issues


def needs_repair(issues):
    return any(not issue.get("minor") for issue in issues)


def repair_message(issues):
    lines = ["The program checked your JSON against the code and found these problems:"]
    for issue in issues:
        where = issue["field"] + (f' "{issue["item"]}"' if issue["item"] else "")
        lines.append(f"- {where}: {issue['problem']}")
    lines.append("Return the complete corrected JSON with all eight fields. Rewrite or remove only the items "
                 "listed above and keep every other item exactly as it was. Use [] for a list that becomes empty.")
    return "\n".join(lines)
