"""Shared by the AI code: where its settings live, its error type, JSON files and hashes."""
import hashlib
import json
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent / "settings"  # models, limits, prompts and the topic list


class AIError(Exception):
    """A problem with the AI settings, the model or its answer; code is a short reason such as "missing_model"."""

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
        raise AIError(f"Cannot read JSON file {path}: {error}", "config_error") from error
