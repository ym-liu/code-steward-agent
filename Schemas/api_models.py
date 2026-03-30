"""
MVP REST payload shapes (requests/responses). Enforced via Pydantic + FastAPI response_model.

Artifact IDs (recommended):
    Use a 64-char lowercase hex SHA-256 of the UTF-8 normalized absolute path
    (e.g. resolve, normcase on Windows, forward slashes optional in canonical form).
    Deterministic without reading file bytes; optional later: content-hash ID for dedupe.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Shared / errors
# ---------------------------------------------------------------------------


class ValidationErrorItem(BaseModel):
    """Single field-level validation message (e.g. invalid config)."""

    model_config = ConfigDict(extra="forbid")

    field: str
    message: str


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Liveness/readiness for monitors."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded", "down"] = "ok"
    uptime_seconds: float = 0.0
    version: str = "0.0.0"


class SystemStatusResponse(BaseModel):
    """High-level agent + loop state; `internal_state` is for tests/observability."""

    model_config = ConfigDict(extra="forbid")

    agent_running: bool = True
    agent_mode: Literal["background"] = "background"
    loops: dict[str, bool] = Field(
        default_factory=lambda: {
            "scanner": False,
            "file_watcher": False,
            "api": True,
        },
        description="Which subsystems are active (noop defaults).",
    )
    scan_in_progress: bool = False
    graceful_shutdown_requested: bool = False
    internal_state: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque key/value snapshot for validation tests (deterministic noop).",
    )


class ShutdownRequest(BaseModel):
    """Request graceful stop of background work (implementation may noop)."""

    model_config = ConfigDict(extra="forbid")

    graceful: bool = Field(default=True, description="Flush DB/logs before exit when implemented.")


class ShutdownResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool = False
    message: str = "Shutdown not implemented (noop)."


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


class ScanTriggerRequest(BaseModel):
    """Optional body for manual scan; useful for test annotations."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class ScanStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_progress: bool = False
    watched_paths: list[str] = Field(
        default_factory=list,
        description="Configured directories actively watched (noop: empty).",
    )
    last_scan_started_at: datetime | None = None
    last_scan_finished_at: datetime | None = None
    last_scan_kind: Literal["manual", "periodic", "event_driven", "none"] = "none"
    pending_manual_triggers: int = 0


class ScanTriggerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool = False
    message: str | None = Field(default=None, description="Human-readable outcome or queue position.")


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


class ArtifactTimestamps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_at: datetime | None = None
    modified_at: datetime | None = None


class ArtifactMetadata(BaseModel):
    """Full metadata for one discovered file (any type)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Stable ID, e.g. sha256 hex of normalized path.")
    path: str
    filename: str
    extension: str | None = Field(default=None, description="Leading dot optional; null if none.")
    timestamps: ArtifactTimestamps = Field(default_factory=ArtifactTimestamps)
    size_bytes: int = Field(default=0, ge=0)
    discovery_source: Literal[
        "periodic_scan",
        "file_event",
        "manual_scan",
        "startup",
        "unknown",
    ] = "unknown"


class ArtifactListItem(BaseModel):
    """Minimal row when client requests a subset of fields."""

    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    filename: str


class ArtifactListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ArtifactListItem] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    limit: int
    offset: int = Field(default=0, ge=0)


class ArtifactDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    artifact: ArtifactMetadata | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_system_paths: bool = True
    max_cpu_percent: float | None = Field(default=25.0, ge=0.0, le=100.0)
    max_memory_mb: int | None = Field(default=512, ge=0)


class SafetyConfigUpdate(BaseModel):
    """Partial PATCH for `safety`; only set fields override."""

    model_config = ConfigDict(extra="forbid")

    block_system_paths: bool | None = None
    max_cpu_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    max_memory_mb: int | None = Field(default=None, ge=0)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    idle_backoff_seconds: int = Field(default=5, ge=0)


class RuntimeConfigUpdate(BaseModel):
    """Partial PATCH for `runtime`."""

    model_config = ConfigDict(extra="forbid")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None
    idle_backoff_seconds: int | None = Field(default=None, ge=0)


class ConfigSnapshot(BaseModel):
    """
    Full effective configuration (JSON file mirror).
    PATCH merges into this; file is never deleted/recreated by the API.
    """

    model_config = ConfigDict(extra="forbid")

    scan_directories: list[str] = Field(default_factory=list)
    dedupe_directory: str | None = Field(
        default=None,
        description="Future: copy organized artifacts here; MVP noop.",
    )
    allow_list: list[str] = Field(default_factory=list)
    deny_list: list[str] = Field(default_factory=list)
    scan_interval: int = Field(default=60, ge=1, description="Seconds between periodic scans.")
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    deduplication_enabled: bool = Field(default=False, description="MVP noop toggle for tests.")
    sample_max_bytes: int | None = Field(default=None, ge=0, description="Future sample extraction cap.")
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @field_validator("scan_directories", "allow_list", "deny_list")
    @classmethod
    def _strip_paths(cls, v: list[str]) -> list[str]:
        return [p.strip() for p in v if p.strip()]


class ConfigUpdateRequest(BaseModel):
    """Partial update; omitted keys keep previous values."""

    model_config = ConfigDict(extra="forbid")

    scan_directories: list[str] | None = None
    dedupe_directory: str | None = None
    allow_list: list[str] | None = None
    deny_list: list[str] | None = None
    scan_interval: int | None = Field(default=None, ge=1)
    safety: SafetyConfigUpdate | None = None
    deduplication_enabled: bool | None = None
    sample_max_bytes: int | None = Field(default=None, ge=0)
    runtime: RuntimeConfigUpdate | None = None


class ConfigUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    message: str | None = None
    errors: list[ValidationErrorItem] = Field(default_factory=list)
    config: ConfigSnapshot


class ConfigReloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reloaded: bool = False
    message: str = "Reload applied (noop: in-memory only until file IO exists)."
    config: ConfigSnapshot


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


class LogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: datetime
    level: str
    message: str
    category: str | None = Field(
        default=None,
        description="e.g. scan, discovery, config, blocked, error.",
    )


class LogsListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logs: list[LogEntry] = Field(default_factory=list)
    limit: int
    total: int = Field(default=0, ge=0)
    has_more: bool = False
