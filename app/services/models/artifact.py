from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class ArtifactKind(StrEnum):
    SCRIPT = "script"
    WRAPPER = "wrapper"
    SOURCE = "source"
    BINARY = "binary"
    CONFIG = "config"
    DATA = "data"
    DOCUMENTATION = "documentation"
    UNKNOWN = "unknown"


class ArtifactStatus(StrEnum):
    ACTIVE = "active"
    MISSING = "missing"
    UNTRACKED = "untracked"
    DELETED = "deleted"


@dataclass(kw_only=True)
class Artifact:
    # Identity
    id: str

    # Location
    root: Path
    relative_path: Path

    # File on disk
    size: int
    modified_at: datetime
    hash: str | None = None  # None for files over the size limit
    error: str | None = None  # None if no error

    # Classification
    kind: ArtifactKind = ArtifactKind.UNKNOWN
    classifier_version: int = 0

    # Lifecycle
    status: ArtifactStatus = ArtifactStatus.ACTIVE
    status_changed_at: datetime
    first_seen: datetime
