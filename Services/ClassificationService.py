"""Program-only file classification: no AI, no running of files.

Every file gets one of the requirement types (wrapper, script, binary, sample, data, documentation,
unknown), a language or format, binary and sensitive flags, and a confidence with its reasons.
The confidence comes from how clear the evidence is; it is computed here, never reported by a model.
The rules match the AI lab prototype (ai_lab/organize.py) without its AI parts.

When a rule changes, raise CLASSIFIER_VERSION: saved files are classified again on the next scan.
"""
import os
import re

CLASSIFIER_VERSION = 1
HEAD_BYTES = 65536  # only the start of a file is read for classification

LANGUAGES = {
    ".py": "Python", ".pyw": "Python", ".sh": "Shell script", ".bash": "Shell script", ".zsh": "Shell script",
    ".bat": "Windows batch script", ".cmd": "Windows batch script", ".ps1": "PowerShell script",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".rb": "Ruby", ".pl": "Perl", ".php": "PHP", ".lua": "Lua",
    ".r": "R", ".c": "C", ".h": "C/C++ header", ".cc": "C++", ".cpp": "C++", ".cxx": "C++",
    ".hpp": "C++ header", ".cs": "C#", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin script",
    ".go": "Go", ".rs": "Rust", ".swift": "Swift", ".scala": "Scala", ".sql": "SQL", ".awk": "AWK",
    ".sed": "sed script", ".tcl": "Tcl", ".vbs": "VBScript", ".bas": "BASIC", ".ipynb": "Jupyter notebook",
}
FORMATS = {".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".csv": "CSV", ".tsv": "TSV", ".md": "Markdown",
           ".toml": "TOML", ".ini": "INI settings", ".env": "environment settings", ".xml": "XML",
           ".txt": "text", ".png": "PNG image", ".jpg": "JPEG image", ".jpeg": "JPEG image", ".pdf": "PDF"}
DOC_EXTENSIONS = {".md", ".markdown", ".rst", ".adoc", ".pdf", ".doc", ".docx", ".odt", ".rtf", ".tex"}
DATA_EXTENSIONS = {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                   ".conf", ".properties", ".env", ".xlsx", ".xls", ".ods", ".parquet", ".feather", ".h5", ".hdf5",
                   ".npy", ".npz", ".pkl", ".pickle", ".pt", ".pth", ".ckpt", ".onnx", ".safetensors", ".db",
                   ".sqlite", ".sqlite3", ".log", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".svg",
                   ".webp", ".ico", ".mp3", ".wav", ".flac", ".mp4", ".mov", ".avi", ".zip", ".tar", ".gz", ".tgz",
                   ".bz2", ".xz", ".7z", ".rar", ".dat"}
CONFIG_EXTENSIONS = {".env", ".ini", ".cfg", ".conf", ".properties", ".toml", ".yaml", ".yml"}
BINARY_EXTENSIONS = {".exe": "Windows executable", ".dll": "Windows library", ".so": "shared library",
                     ".dylib": "macOS library", ".com": "DOS executable", ".jar": "Java archive",
                     ".class": "Java class", ".msi": "Windows installer", ".o": "object file", ".a": "static library"}
BINARY_MAGIC = ((b"\x7fELF", "ELF executable (Linux)"), (b"MZ", "Windows executable"),
                (b"\xcf\xfa\xed\xfe", "Mach-O executable (macOS)"), (b"\xce\xfa\xed\xfe", "Mach-O executable (macOS)"),
                (b"\xfe\xed\xfa\xcf", "Mach-O executable (macOS)"), (b"\xfe\xed\xfa\xce", "Mach-O executable (macOS)"),
                (b"\xca\xfe\xba\xbe", "Mach-O universal executable or Java class"))
SHEBANG = re.compile(r"^#!\s*\S*?(?:\benv\s+(?:-\S+\s+)*)?\b(python3?|bash|sh|zsh|node|ruby|perl|pwsh|Rscript)\b")
SHEBANG_LANGUAGES = {"python": "Python", "python3": "Python", "bash": "Shell script", "sh": "Shell script",
                     "zsh": "Shell script", "node": "JavaScript", "ruby": "Ruby", "perl": "Perl",
                     "pwsh": "PowerShell script", "Rscript": "R"}
DOC_NAME = re.compile(r"^(?:readme|license|licence|copying|notice|changelog|changes|authors|contributing|history)(?:[._-]|$)", re.I)
SAMPLE_FOLDERS = {"sample", "samples", "example", "examples", "demo", "demos", "fixture", "fixtures", "testdata",
                  "test_data", "test-data", "sample_data", "sample-data"}
SAMPLE_NAME = re.compile(r"(?:^|[._-])(?:sample|example|expected|fixture|demo)s?(?:[._-]|$)", re.I)
CONFIG_NAME = re.compile(r"(?:^|[._-])(?:env|config|configuration|settings|secrets?|credentials?)(?:[._-]|$)", re.I)
CONFIG_CODE = re.compile(r"^(?:local[_-])?(?:config|configuration|settings|secrets?|credentials?)$", re.I)
KEY_FILE = re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)$|\.(?:pem|key|p12|pfx|kdbx|keystore|jks)$|^\.(?:netrc|pgpass|npmrc|pypirc)$", re.I)
SECRET = re.compile(r"""(?i)\b(?:pass(?:word|wd)?|secret(?:_key)?|api[_-]?key|access[_-]?key|auth[_-]?token|token)\b["']?\s*[:=]\s*(["'])([^"'\s]{6,})\1""")
PLACEHOLDER = re.compile(r"(?i)^(?:x+|\*+|\.+|change-?me.*|replace.*|your.*|example.*|dummy.*|placeholder.*|<.*>|\$\{.*\}|%.*%)$")
DATA_LINE = re.compile(r"^(?:https?://\S+|[\w.+-]+@[\w.-]+\.\w+|[-+]?\d[\d.,:;/\s-]*|\S+(?:\s*[,;\t]\s*\S+)+|[\w.~/\\-]+\.\w{1,5})$")
PROSE_WORD = re.compile(r"(?<!\S)[a-z][a-z'-]*[a-z](?!\S)")
COMMENT = re.compile(r"^\s*(?:#|//|--|;|::|rem\b|/\*|\*)", re.I)
# A line that starts another script with an interpreter, or runs a program by its path.
CODE_FILE = r"[\w.~/\\%${}()-]*[\w-]\.(?:py|js|mjs|cjs|ts|r|rb|pl|sh|ps1|bat|cmd|sql|jar|php)\b"
INTERPRETER_RUN = re.compile(r"(?:^|[\s\"'(\[,;&|=])(?:python3?|py|node|rscript|ruby|perl|bash|sh|zsh|pwsh|powershell|java|"
                             rf"php|psql|cmd)(?:\.exe)?\b[^|;\n]*?({CODE_FILE})", re.I)
DIRECT_RUN = re.compile(r"""(?:^|[;&|(]\s*|\b(?:exec|call|start|nohup|source)\s+|^\s*\.\s+)["']?((?:\./|\.\\|%~dp0|\$\{?\w+\}?[/\\]|\$\(dirname[^)]*\)[/\\]|bin[/\\])[\w./\\-]+)""", re.I)


class ClassificationService:
    VERSION = CLASSIFIER_VERSION

    def describe(self, path, head, root=None):
        """Classify one file from its name and the first HEAD_BYTES of its contents.

        root is the scanned folder; folder names above it (such as a root called "samples") are ignored.
        """
        name = os.path.basename(path)
        suffix = os.path.splitext(name)[1].lower() or (name.lower() if name.startswith(".") else "")
        text = None if b"\x00" in head else head.decode("utf-8", errors="replace")
        result = {"artifact_type": "unknown", "language": None, "is_binary": text is None, "sensitive": False,
                  "sensitive_reason": None, "confidence": 0.3, "reasons": [], "classifier_version": CLASSIFIER_VERSION}
        magic = next((label for prefix, label in BINARY_MAGIC if head.startswith(prefix)), None)
        if (magic and b"\x00" in head[:4096]) or suffix in BINARY_EXTENSIONS:
            result.update(artifact_type="binary", language=magic or BINARY_EXTENSIONS[suffix], is_binary=True,
                          confidence=0.95 if magic else 0.85,
                          reasons=[f"Compiled program ({magic or 'by its extension'}); it is never run."])
            return result
        shebang = SHEBANG.match(text or "")
        if suffix in LANGUAGES or (shebang and not suffix):
            language = LANGUAGES.get(suffix) or SHEBANG_LANGUAGES[shebang.group(1)]
            result.update(language=language, **self._code_type(text or ""),
                          confidence=0.85 if suffix in LANGUAGES else 0.7)
            if not suffix:
                result["reasons"].append("Starts with #! so it runs as a script.")
        else:
            result.update(self._other_type(path, name, suffix, text, root))
        reason = self._sensitive_reason(name, suffix, text, code=result["language"] in LANGUAGES.values())
        if reason:
            result.update(sensitive=True, sensitive_reason=reason)
        return result

    @staticmethod
    def _code_type(text):
        """A short script whose main job is starting other programs is a wrapper; other code is a script."""
        lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#!") and not COMMENT.match(line)]
        starts = [line for line in lines if INTERPRETER_RUN.search(line) or DIRECT_RUN.search(line)]
        if starts and (len(lines) <= 15 or len(starts) / len(lines) >= 0.25):
            return {"artifact_type": "wrapper", "reasons": [f"Mainly starts other programs or scripts ({len(starts)} of {len(lines)} lines)."]}
        return {"artifact_type": "script", "reasons": ["Source code by its extension or first line."]}

    @staticmethod
    def _other_type(path, name, suffix, text, root):
        folder = os.path.dirname(path)
        if root:
            folder = os.path.relpath(folder, root) if os.path.abspath(folder) != os.path.abspath(root) else ""
        folders = [part.lower() for part in os.path.normpath(folder).split(os.sep) if part not in ("", ".")]
        language = FORMATS.get(suffix) or ("text" if text is not None else "binary data")
        if DOC_NAME.match(name) or suffix in DOC_EXTENSIONS:
            return {"artifact_type": "documentation", "language": language, "confidence": 0.9,
                    "reasons": ["Documentation by its name or format."]}
        if (suffix in DATA_EXTENSIONS or suffix == ".txt") and (SAMPLE_NAME.search(os.path.splitext(name)[0])
                                                                or SAMPLE_FOLDERS & set(folders[-2:])):
            return {"artifact_type": "sample", "language": language, "confidence": 0.7,
                    "reasons": ["Example data: it is in an examples folder or named as one."]}
        if suffix in DATA_EXTENSIONS:
            return {"artifact_type": "data", "language": language, "confidence": 0.9,
                    "reasons": ["Data, settings or media file by its format."]}
        if text is None:
            return {"artifact_type": "data", "language": language, "confidence": 0.6,
                    "reasons": ["Binary data that is not a program."]}
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and sum(bool(DATA_LINE.match(line)) for line in lines) / len(lines) >= 0.7:
            return {"artifact_type": "data", "language": language, "confidence": 0.7,
                    "reasons": ["Most lines are values such as web addresses, numbers or comma-separated fields."]}
        if lines and sum(len(PROSE_WORD.findall(line)) >= 3 for line in lines) / len(lines) >= 0.3:
            return {"artifact_type": "documentation", "language": language, "confidence": 0.5,
                    "reasons": ["Looks like written notes."]}
        return {"artifact_type": "unknown", "language": language, "confidence": 0.3,
                "reasons": ["No recognizable code, document or data structure."]}

    @staticmethod
    def _sensitive_reason(name, suffix, text, code):
        """Configuration and environment files are sensitive by default (requirement), as are keys and passwords."""
        if KEY_FILE.search(name):
            return "Private key or password-store file."
        stem = os.path.splitext(name)[0] if not name.startswith(".") else name
        if CONFIG_CODE.match(stem) if code else (suffix in CONFIG_EXTENSIONS or CONFIG_NAME.search(stem)):
            return "Configuration or environment file (sensitive by default)."
        for match in SECRET.finditer(text or ""):
            if not PLACEHOLDER.match(match.group(2)):
                return "Contains what looks like a password, key or token."
        return None
