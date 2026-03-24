from fastapi import APIRouter

from Services.ServiceContainer import services

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("")
def list_artifacts():
    return services.artifacts_service.list_artifacts()


@router.get("/{artifact_id}")
def get_artifact(artifact_id: str):
    return services.artifacts_service.get_artifact(artifact_id)
