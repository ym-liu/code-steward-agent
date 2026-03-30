"""Scan orchestration (noop): shapes match periodic + file-event design."""

from __future__ import annotations

from Schemas.api_models import ScanStatusResponse, ScanTriggerRequest, ScanTriggerResponse


class ScanService:
    def __init__(self, config_service, logs_service):
        self.config_service = config_service
        self._logs = logs_service

    def get_status(self) -> ScanStatusResponse:
        paths = list(self.config_service.get_config().scan_directories)
        return ScanStatusResponse(
            in_progress=False,
            watched_paths=paths,
            last_scan_started_at=None,
            last_scan_finished_at=None,
            last_scan_kind="none",
            pending_manual_triggers=0,
        )

    def enqueue_manual_scan(self, payload: ScanTriggerRequest) -> ScanTriggerResponse:
        self._logs.append_log(
            level="INFO",
            message="Manual scan requested (noop).",
            category="scan",
        )
        return ScanTriggerResponse(accepted=False, message="Scan queue not implemented (noop).")
