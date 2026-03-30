"""Discovered artifacts: list (filters) and get-by-id."""

from __future__ import annotations

from fastapi import APIRouter, Query

from Schemas.api_models import ArtifactDetailResponse, ArtifactListResponse
from Services.ServiceContainer import services

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


def _parse_fields(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    allowed = {"id", "path", "filename"}
    unknown = set(parts) - allowed
    if unknown:
        # Keep validation simple: ignore unknown tokens for noop, or strip to allowed
        parts = [p for p in parts if p in allowed]
    return parts or None


@router.get(
    "",
    response_model=ArtifactListResponse,
    summary="List artifacts with pagination and filters",
)
def list_artifacts(
    limit: int = Query(50, ge=1, le=500, description="Page size."),
    offset: int = Query(0, ge=0),
    path_prefix: str | None = Query(None, description="Filter paths starting with this prefix."),
    extension: str | None = Query(
        None,
        description="Filter by extension (with or without leading dot).",
    ),
    fields: str | None = Query(
        None,
        description="Comma-separated subset: id, path, filename (future projection).",
    ),
) -> ArtifactListResponse:
    return services.artifacts_service.list_artifacts(
        limit=limit,
        offset=offset,
        path_prefix=path_prefix,
        extension=extension,
        fields=_parse_fields(fields),
    )


@router.get(
    "/{artifact_id}",
    response_model=ArtifactDetailResponse,
    summary="Get one artifact by ID",
)
def get_artifact(artifact_id: str) -> ArtifactDetailResponse:
    return services.artifacts_service.get_artifact(artifact_id)
