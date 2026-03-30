"""System health and observable internal state (noop values until runtime wiring)."""

from __future__ import annotations

from Schemas.api_models import HealthResponse, ShutdownResponse, SystemStatusResponse


class SystemService:
    def __init__(self, scan_service):
        self.scan_service = scan_service

    def get_health(self) -> HealthResponse:
        return HealthResponse(status="ok", uptime_seconds=0.0, version="0.0.0")

    def get_status(self) -> SystemStatusResponse:
        scan = self.scan_service.get_status()
        return SystemStatusResponse(
            agent_running=True,
            scan_in_progress=scan.in_progress,
            internal_state={"noop": True, "scan_pending_manual": scan.pending_manual_triggers},
        )

    def request_shutdown(self, graceful: bool) -> ShutdownResponse:
        return ShutdownResponse(
            accepted=False,
            message="Shutdown not implemented (noop).",
        )
