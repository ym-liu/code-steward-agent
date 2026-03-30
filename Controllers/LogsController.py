"""Structured logs for scans, config, discoveries, blocked ops, errors."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from Schemas.api_models import LogsListResponse
from Services.ServiceContainer import services

router = APIRouter(prefix="/logs", tags=["logs"])


def _parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


@router.get(
    "",
    response_model=LogsListResponse,
    summary="Query log entries",
)
def get_logs(
    limit: int = Query(100, ge=1, le=1000),
    level: str | None = Query(None, description="Filter by level, e.g. INFO, ERROR"),
    category: str | None = Query(None, description="Filter by category, e.g. scan, config, discovery"),
    since: str | None = Query(
        None,
        description="ISO-8601 lower bound (e.g. 2026-03-28T12:00:00Z).",
    ),
) -> LogsListResponse:
    return services.logs_service.list_logs(
        limit=limit,
        level=level,
        category=category,
        since=_parse_since(since),
    )
