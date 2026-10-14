"""Read one code file for the model: its text, hash and program facts."""
from ..common import AIError, digest
from .facts import static_facts

EXTENSIONS = {".py", ".sh", ".bat", ".ps1", ".js", ".jsx", ".ts", ".tsx", ".rb", ".pl", ".txt",
              ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".java", ".go", ".rs",
              ".php", ".r", ".lua", ".swift", ".sql", ".kt", ".kts"}


def snapshot(path, label, max_chars, rubric=None):
    case = {"name": label, "rubric": rubric or {}, "source_path": str(path.resolve())}
    try:
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise AIError("Linked files are skipped.", "input_link")
        if path.suffix.lower() not in EXTENSIONS:
            raise AIError(f"Unsupported extension {path.suffix!r}.", "input_extension")
        with path.open("rb") as stream:
            raw = stream.read(max_chars * 4 + 1)
        if len(raw) > max_chars * 4:
            raise AIError("File exceeds the input budget; use a smaller file.", "input_too_large")
        source = raw.decode("utf-8-sig")
        if "\x00" in source:
            raise AIError("Binary input is not supported.", "input_binary")
        if not source.strip():
            raise AIError("Empty input.", "input_empty")
        if len(source) > max_chars:
            raise AIError(f"File has {len(source)} characters; limit is {max_chars}. No truncation was applied.", "input_too_large")
        case.update(source=source, source_hash=digest(raw), facts=static_facts(source, path.suffix.lower()))
    except (OSError, UnicodeError, AIError) as error:
        case["input_error"] = str(error)
        case["input_error_code"] = (error.code if isinstance(error, AIError) else
                                    "input_encoding" if isinstance(error, UnicodeError) else "input_unreadable")
    return case
