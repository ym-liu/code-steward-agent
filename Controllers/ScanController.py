"""Scan: status + manual trigger (testing)."""

from __future__ import annotations

from fastapi import APIRouter, Body

from Schemas.api_models import ScanStatusResponse, ScanTriggerRequest, ScanTriggerResponse
from Services.ServiceContainer import services

router = APIRouter(prefix="/scan", tags=["scan"])


@router.get(
    "/status",
    response_model=ScanStatusResponse,
    summary="Current scan / watcher status",
)
def get_scan_status() -> ScanStatusResponse:
    return services.scan_service.get_status()


@router.post(
    "",
    response_model=ScanTriggerResponse,
    summary="Trigger a manual scan (primary)",
)
def post_scan(
    payload: ScanTriggerRequest | None = Body(default=None),
) -> ScanTriggerResponse:
    body = payload or ScanTriggerRequest()
    return services.scan_service.enqueue_manual_scan(body)


@router.post(
    "/trigger",
    response_model=ScanTriggerResponse,
    summary="Alias for POST /scan (manual trigger)",
)
def post_scan_trigger(
    payload: ScanTriggerRequest | None = Body(default=None),
) -> ScanTriggerResponse:
    body = payload or ScanTriggerRequest()
    return services.scan_service.enqueue_manual_scan(body)
