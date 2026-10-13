from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from Services.ServiceContainer import services

router = APIRouter(prefix="/artifacts", tags=["artifacts"])
ArtifactType = Literal["wrapper", "script", "binary", "sample", "data", "documentation", "unknown"]


@router.get("")
def list_artifacts(
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum artifacts to return."),
    offset: int = Query(default=0, ge=0, description="Artifacts to skip, for the next page."),
    artifact_type: ArtifactType | None = Query(default=None, description="Only this file type."),
    language: str | None = Query(default=None, max_length=100, description="Only this language or format, e.g. Python."),
    sensitive: bool | None = Query(default=None, description="Only sensitive (true) or non-sensitive (false) files."),
    path_contains: str | None = Query(default=None, max_length=500, description="Only paths containing this text."),
):
    """One page of saved artifacts. total is the number of all matches; use offset for the next page."""
    return services.artifacts_service.search_artifacts(limit=limit, offset=offset, artifact_type=artifact_type,
                                                       language=language, sensitive=sensitive,
                                                       path_contains=path_contains)


@router.get("/{artifact_id}", responses={404: {"description": "No artifact has this ID."}})
def get_artifact(artifact_id: str):
    """One artifact with its classification, plus other files with exactly the same contents."""
    artifact = services.artifacts_service.find_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return {**artifact, "same_content_as": services.artifacts_service.same_content(artifact)}
