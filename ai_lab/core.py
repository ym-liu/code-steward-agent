"""Input snapshots and a small, dependency-free structured analysis contract."""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB = Path(__file__).resolve().parent
PROMPT = (LAB / "prompt.txt").read_text(encoding="utf-8").strip()
SCHEMA_VERSION = "1"
TEXT_LIST = {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 12}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "purpose": {"type": ["string", "null"], "minLength": 1},
        "inputs": TEXT_LIST, "outputs": TEXT_LIST,
        "dependencies": TEXT_LIST, "unknowns": TEXT_LIST,
        "evidence": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"line": {"type": "integer", "minimum": 1},
                           "claim": {"type": "string", "minLength": 1}},
            "required": ["line", "claim"],
        }},
    },
    "required": ["purpose", "inputs", "outputs", "dependencies", "unknowns", "evidence"],
}
EXTENSIONS = {".py", ".sh", ".bat", ".ps1", ".js", ".ts", ".rb", ".pl", ".txt"}
EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", "ai-results"}
CONFIG_KEYS = {"base_url", "models", "default_model", "device", "timeout_seconds", "num_ctx",
               "num_predict", "num_thread", "temperature", "seed", "max_source_chars", "max_files"}


class LabError(Exception):
    def __init__(self, message, code="error"):
        super().__init__(message)
        self.code = code


def digest(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise LabError(f"Cannot read JSON file {path}: {error}", "config_error") from error


def load_config(path=None):
    config = read_json(path or LAB / "config.json")
    if not isinstance(config, dict):
        raise LabError("Config must be a JSON object.", "config_error")
    if set(config) != CONFIG_KEYS:
        raise LabError(f"Config keys must be: {', '.join(sorted(CONFIG_KEYS))}", "config_error")
    limits = {"timeout_seconds": (1, 3600), "num_ctx": (1024, 32768),
              "num_predict": (64, 4096), "num_thread": (1, 128),
              "seed": (0, 2**31 - 1), "max_source_chars": (100, 100000), "max_files": (1, 1000)}
    for key, (low, high) in limits.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise LabError(f"{key} must be an integer from {low} to {high}.", "config_error")
    if config["num_predict"] >= config["num_ctx"]:
        raise LabError("num_predict must be smaller than num_ctx.", "config_error")
    if type(config["temperature"]) not in (int, float) or not 0 <= config["temperature"] <= 2:
        raise LabError("temperature must be between 0 and 2.", "config_error")
    if config["device"] not in ("cpu", "auto"):
        raise LabError("device must be cpu or auto.", "config_error")
    models = config["models"]
    if not isinstance(models, dict) or not models or not all(isinstance(k, str) for k in models):
        raise LabError("models must contain aliases and Ollama model tags.", "config_error")
    for tag in models.values():
        validate_model(tag)
    if not isinstance(config["default_model"], str) or config["default_model"] not in models:
        raise LabError("default_model must be an alias in models.", "config_error")
    return config


def validate_model(tag):
    if not isinstance(tag, str) or not tag or len(tag) > 200 or any(c.isspace() for c in tag):
        raise LabError("Use a non-empty Ollama model tag without spaces.", "config_error")
    if "cloud" in tag.lower() or "://" in tag or any(ord(c) < 32 for c in tag):
        raise LabError("This lab accepts local Ollama models only, not cloud models/URLs.", "config_error")
    return tag


def resolve_model(config, name):
    return validate_model(config["models"].get(name, name))


def static_facts(source, suffix):
    facts = {"extension": suffix, "line_count": len(source.splitlines()), "imports": []}
    if suffix == ".py":
        try:
            tree = ast.parse(source)
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(n.name for n in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add("." * node.level + (node.module or ""))
            facts["imports"] = sorted(imports)
        except (SyntaxError, ValueError, RecursionError):
            facts["parse_error"] = "Python syntax could not be parsed; imports are incomplete."
    return facts


def snapshot(path, label, max_chars, rubric=None):
    case = {"name": label, "rubric": rubric or {}, "source_path": str(path.resolve())}
    try:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise LabError("Linked files are skipped.", "input_link")
        if path.suffix.lower() not in EXTENSIONS:
            raise LabError(f"Unsupported extension {path.suffix!r}.", "input_extension")
        with path.open("rb") as stream:
            raw = stream.read(max_chars * 4 + 1)
        if len(raw) > max_chars * 4:
            raise LabError("File exceeds the input budget; use a smaller file.", "input_too_large")
        source = raw.decode("utf-8-sig")
        if "\x00" in source:
            raise LabError("Binary input is not supported.", "input_binary")
        if not source.strip():
            raise LabError("Empty input.", "input_empty")
        if len(source) > max_chars:
            raise LabError(f"File has {len(source)} characters; limit is {max_chars}. No truncation was applied.", "input_too_large")
        case.update(source=source, source_hash=digest(raw), facts=static_facts(source, path.suffix.lower()))
    except (OSError, UnicodeError, LabError) as error:
        case["input_error"] = str(error)
        case["input_error_code"] = (error.code if isinstance(error, LabError) else
                                    "input_encoding" if isinstance(error, UnicodeError) else "input_unreadable")
    return case


def collect_cases(input_path, config):
    if input_path is None:
        manifest = read_json(LAB / "samples" / "manifest.json")
        cases = [snapshot(LAB / "samples" / item["file"], item["file"],
                          config["max_source_chars"], item["rubric"]) for item in manifest]
    else:
        base = Path(input_path).expanduser().absolute()
        if not base.exists():
            raise LabError(f"Input not found: {base}", "input_error")
        if base.is_symlink() or getattr(base, "is_junction", lambda: False)():
            raise LabError("Choose a real file/folder, not a link.", "input_error")
        if base.is_file():
            cases = [snapshot(base, base.name, config["max_source_chars"])]
        else:
            import os
            paths = []
            for directory, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in EXCLUDED
                                 and not (Path(directory) / d).is_symlink()
                                 and not getattr(Path(directory) / d, "is_junction", lambda: False)())
                for name in sorted(files):
                    path = Path(directory) / name
                    if path.suffix.lower() in EXTENSIONS:
                        paths.append(path)
                        if len(paths) > config["max_files"]:
                            raise LabError("Too many input files; select a smaller folder or raise max_files.", "input_error")
            cases = [snapshot(p, p.relative_to(base).as_posix(), config["max_source_chars"]) for p in sorted(paths)]
    if not cases:
        raise LabError("No supported text files found in the selected input.", "input_error")
    return cases


def messages_for(case):
    # Reference answers are deliberately never included in model input.
    numbered = "\n".join(f"{n}: {line}" for n, line in enumerate(case["source"].splitlines(), 1))
    payload = {"file": case["name"], "facts": case["facts"], "numbered_source": numbered}
    return [{"role": "system", "content": PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def validate_analysis(raw, line_count):
    """Validate shape AND line bounds. Semantic correctness still needs a human."""
    try:
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"Duplicate JSON key: {key}")
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique_keys,
                           parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, TypeError) as error:
        return None, False, False, [f"Invalid JSON: {error}"]
    errors = []
    if not isinstance(value, dict) or set(value) != set(SCHEMA["required"]):
        return value, True, False, ["Output must contain exactly the six required fields."]
    if value["purpose"] is not None and (not isinstance(value["purpose"], str) or not value["purpose"].strip()):
        errors.append("purpose must be a non-empty string or null.")
    for key in ("inputs", "outputs", "dependencies", "unknowns"):
        items = value[key]
        if not isinstance(items, list) or len(items) > 12 or not all(isinstance(s, str) and s.strip() for s in items):
            errors.append(f"{key} must be an array of at most 12 non-empty strings.")
    evidence = value["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 6:
        errors.append("evidence must be an array of at most 6 items.")
    else:
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"line", "claim"}:
                errors.append("Evidence items need exactly line and claim.")
            elif type(item["line"]) is not int or item["line"] < 1 or not isinstance(item["claim"], str) or not item["claim"].strip():
                errors.append("Evidence line/claim has an invalid type or value.")
    schema_valid = not errors
    if schema_valid:
        if any(item["line"] > line_count for item in evidence):
            errors.append("Evidence cites a line outside the input file.")
        if value["purpose"] is not None and not evidence:
            errors.append("A stated purpose needs at least one evidence item.")
        if value["purpose"] is None and not value["unknowns"]:
            errors.append("Unknown purpose must explain what is missing in unknowns.")
    return value, True, schema_valid, errors
