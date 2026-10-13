"""Shared by every part of the AI lab: where its files live, its error type, JSON files and hashes."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repository folder
LAB = Path(__file__).resolve().parent  # ai_lab/
SETTINGS = LAB / "settings"  # files people edit: run settings, prompts and the topic list
SAMPLES = LAB / "samples"  # default test folder: put code to test here
ANSWER_KEYS = LAB / "answer_keys"  # reference answers for the samples; never sent to a model
BENCHMARKS = LAB / "benchmarks"  # labeled dev / holdout / robustness sets
RESULTS = ROOT / "ai-results"  # one folder per run; ignored by Git


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


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
