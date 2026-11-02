from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ScanTrigger(StrEnum):
    STARTUP = "startup"
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    FOLDER_ADDED = "folder_added"


class ScanStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(kw_only=True)
class ScanRun:
    # Identity
    id: str

    # Run
    trigger: ScanTrigger
    started_at: datetime
    finished_at: datetime | None = None  # None while running

    # Result
    status: ScanStatus = ScanStatus.RUNNING
    error: str | None = None  # None if no error

    # Counts (files per change)
    created: int = 0
    modified: int = 0
    moved: int = 0
    deleted: int = 0
    skipped: int = 0
