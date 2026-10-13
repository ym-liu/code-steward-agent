"""Find the supported code files in a folder and read each one (text, hash and program facts)."""
from pathlib import Path

from ..common import ANSWER_KEYS, SAMPLES, LabError, digest, read_json
from .facts import static_facts

EXTENSIONS = {".py", ".sh", ".bat", ".ps1", ".js", ".jsx", ".ts", ".tsx", ".rb", ".pl", ".txt",
              ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".java", ".go", ".rs",
              ".php", ".r", ".lua", ".swift", ".sql", ".kt", ".kts"}
EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", "ai-results"}


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
        cases = collect_cases(SAMPLES, config)
        # The answer keys supply reference checklists, not the list of files to test.
        manifest_path = ANSWER_KEYS / "explanation_checklists.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else []
        references = {Path(item["file"]).as_posix(): item for item in manifest}
        for case in cases:
            reference = references.get(case["name"], {})
            case["rubric"] = reference.get("rubric", {})
            case.update({key: reference[key] for key in ("case_id", "category", "difficulty", "family") if key in reference})
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
