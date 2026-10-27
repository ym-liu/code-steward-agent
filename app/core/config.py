from pathlib import Path
from typing import Annotated

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _require_full_path(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError(f"must be a full path, e.g. ~/scripts or C:/scripts, got {path}")
    return path


FullPath = Annotated[Path, AfterValidator(_require_full_path)]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # What to scan
    watched_folders: list[FullPath] = Field(min_length=1)
    deny_paths: list[FullPath] = Field(default_factory=list)
    exclude_folders: list[str] = Field(default_factory=lambda: [".git", "node_modules", "__pycache__", ".venv"])
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
