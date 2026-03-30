"""
Artifact listing/detail (noop empty).

Artifact ID: prefer 64-char hex SHA-256(normalized absolute path UTF-8); see Schemas.api_models docstring.
"""

from __future__ import annotations

from Schemas.api_models import ArtifactDetailResponse, ArtifactListResponse


class ArtifactsService:
    def __init__(self):
        pass

    def list_artifacts(
        self,
        *,
        limit: int,
        offset: int,
        path_prefix: str | None,
        extension: str | None,
        fields: list[str] | None,
    ) -> ArtifactListResponse:
        # `fields` reserved for future projection; list items stay minimal for MVP.
        _ = path_prefix, extension, fields
        return ArtifactListResponse(items=[], total=0, limit=limit, offset=offset)

    def get_artifact(self, artifact_id: str) -> ArtifactDetailResponse:
        _ = artifact_id
        return ArtifactDetailResponse(found=False, artifact=None)
