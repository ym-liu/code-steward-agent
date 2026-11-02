from pathlib import Path
from typing import Annotated

import yaml
from pathspec import GitIgnoreSpec
from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _require_full_path(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError(f"must be a full path, e.g. ~/scripts or C:/scripts, got {path}")
    return path


def _require_ignore_pattern(pattern: str) -> str:
    GitIgnoreSpec.from_lines([pattern])  # raises ValueError if invalid
    return pattern


FullPath = Annotated[Path, AfterValidator(_require_full_path)]
IgnorePattern = Annotated[str, AfterValidator(_require_ignore_pattern)]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # What to scan
    watched_folders: list[FullPath] = Field(min_length=1)
    deny_paths: list[FullPath] = []
    ignore: list[IgnorePattern] = [".git/", "node_modules/", "__pycache__/", ".venv/"]
    max_file_size_mb: int = Field(default=100, gt=0)

    # When to scan
    full_scan_interval_days: int = Field(default=30, gt=0)

    # Where to save
    database_path: FullPath = Path.home() / ".code-kamaji" / "kamaji.sqlite3"


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Copy config.example.yaml to {path} and edit it.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
