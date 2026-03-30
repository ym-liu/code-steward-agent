"""System: health, status, optional graceful shutdown (noop)."""

from __future__ import annotations

from fastapi import APIRouter, Body

from Schemas.api_models import HealthResponse, ShutdownRequest, ShutdownResponse, SystemStatusResponse
from Services.ServiceContainer import services

router = APIRouter(prefix="/system", tags=["system"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness / readiness",
)
def get_health() -> HealthResponse:
    return services.system_service.get_health()


@router.get(
    "/status",
    response_model=SystemStatusResponse,
    summary="Agent and subsystem status (includes test-oriented internal_state)",
)
def get_status() -> SystemStatusResponse:
    return services.system_service.get_status()


@router.post(
    "/shutdown",
    response_model=ShutdownResponse,
    summary="Request graceful shutdown (implementation may noop)",
)
def post_shutdown(
    body: ShutdownRequest | None = Body(default=None),
) -> ShutdownResponse:
    """Safe shutdown is exposed for tests; production packaging may map this to process exit."""
    payload = body or ShutdownRequest()
    return services.system_service.request_shutdown(graceful=payload.graceful)
