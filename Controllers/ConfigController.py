"""Configuration: read, partial update (PATCH/PUT), reload."""

from __future__ import annotations

from fastapi import APIRouter

from Schemas.api_models import ConfigReloadResponse, ConfigSnapshot, ConfigUpdateRequest, ConfigUpdateResponse
from Services.ServiceContainer import services

router = APIRouter(prefix="/config", tags=["config"])


@router.get(
    "",
    response_model=ConfigSnapshot,
    summary="Full effective configuration",
)
def get_config() -> ConfigSnapshot:
    return services.config_service.get_config()


@router.patch(
    "",
    response_model=ConfigUpdateResponse,
    summary="Partial configuration update (merge into existing snapshot)",
)
def patch_config(body: ConfigUpdateRequest) -> ConfigUpdateResponse:
    """Never deletes the config file; merges into the current snapshot (file IO added later)."""
    return services.config_service.patch_config(body)


@router.put(
    "",
    response_model=ConfigUpdateResponse,
    summary="Partial configuration update (same as PATCH)",
)
def put_config(body: ConfigUpdateRequest) -> ConfigUpdateResponse:
    return services.config_service.patch_config(body)


@router.post(
    "/reload",
    response_model=ConfigReloadResponse,
    summary="Reload configuration from disk (noop until persistence exists)",
)
def reload_config() -> ConfigReloadResponse:
    return services.config_service.reload()
