"""Append-only in-memory log ring (swap for SQLite later)."""

from __future__ import annotations

from datetime import datetime, timezone

from Schemas.api_models import LogEntry, LogsListResponse


class LogsService:
    def __init__(self, max_entries: int = 2000):
        self._max = max_entries
        self._entries: list[LogEntry] = []

    def append_log(self, level: str, message: str, category: str | None = None) -> LogEntry:
        entry = LogEntry(
            time=datetime.now(timezone.utc),
            level=level.upper(),
            message=message,
            category=category,
        )
        self._entries.append(entry)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max :]
        return entry

    def list_logs(
        self,
        *,
        limit: int = 100,
        level: str | None = None,
        category: str | None = None,
        since: datetime | None = None,
    ) -> LogsListResponse:
        rows = list(self._entries)
        if level:
            lv = level.upper()
            rows = [r for r in rows if r.level.upper() == lv]
        if category:
            rows = [r for r in rows if (r.category or "").lower() == category.lower()]
        if since is not None:
            rows = [r for r in rows if r.time >= since]
        total = len(rows)
        tail = rows[-limit:] if limit > 0 else []
        has_more = total > len(tail)
        return LogsListResponse(logs=tail, limit=limit, total=total, has_more=has_more)
