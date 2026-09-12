from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from Services.ServiceContainer import services

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
router = APIRouter(prefix="/logs", tags=["logs"])


class LogEntry(BaseModel):
    timestamp: str = Field(description="New entries use UTC with an offset. Legacy entries retain local time without an offset.")
    level: LogLevel
    source: str | None = Field(description="Originating module; null for legacy entries without a recorded source.")
    message: str = Field(description="Human-readable event, result or error, including multiline exception details.")


class LogsResponse(BaseModel):
    items: list[LogEntry]
    count: int = Field(description="Number of entries returned, not the total size of the log history.")
    limit: int
    level: LogLevel | None
    search: str | None
    model_config = ConfigDict(json_schema_extra={"example": {
        "items": [{"timestamp": "2026-09-11T18:30:00.000+00:00", "level": "INFO",
                   "source": "code-steward.scanner", "message": "Scan complete. 2 new/changed files found."}],
        "count": 1, "limit": 20, "level": None, "search": None,
    }})


@router.get("", response_model=LogsResponse, summary="Read recent application logs",
            responses={503: {"description": "The log file could not be read."}})
def get_logs(
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum matching entries to return, newest first."),
    level: LogLevel | None = Query(default=None, description="Optional exact severity filter; omit to include all levels."),
    search: str | None = Query(default=None, max_length=200, description="Optional case-insensitive text contained in the message."),
):
    """Read real persisted service logs. Filters apply before the limit.

    An empty list means no matching entries. Refresh with Execute to see new events.
    Historical logs remain available across server restarts.
    """
    try:
        return services.logs_service.list_logs(limit=limit, level=level, search=search)
    except OSError as error:
        raise HTTPException(status_code=503, detail="Application logs are temporarily unavailable.") from error
