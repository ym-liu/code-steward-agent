from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from Services.ServiceContainer import services

router = APIRouter(prefix="/analysis", tags=["analysis"])
NOT_FOUND = {404: {"description": "No artifact has this ID."}}


@router.get("/status")
def get_analysis_status():
    """Whether AI explanations are on, the model, what is being explained, how many files wait and an
    estimate of the time left. state says why it waits, for example when Ollama is not running."""
    return services.analysis_service.status()


@router.get("/{artifact_id}", responses=NOT_FOUND)
def get_analysis(artifact_id: str):
    """The AI explanation of one file (purpose, inputs, outputs, usage, dependencies, related files,
    unknowns, evidence), or its place in the queue. status is done, queued, being explained, failed,
    skipped, not queued, not analyzed or off. previous holds an answer from an earlier model or prompt."""
    artifact = services.artifacts_service.find_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return {"artifact_id": artifact_id, "path": artifact["path"], **services.analysis_service.analysis_for(artifact)}


@router.post("/{artifact_id}", status_code=202,
             responses={**NOT_FOUND, 409: {"description": "AI explanations are off or this file type is not explained."}})
def explain_again(artifact_id: str):
    """Queue one file again, even when a saved explanation exists (for example after a failure)."""
    artifact = services.artifacts_service.find_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    analysis = services.analysis_service
    if not analysis.enabled:
        return JSONResponse(status_code=409, content={"detail": "AI explanations are off (ai.enabled in config.yaml)."})
    if artifact.get("artifact_type") not in analysis.types or not artifact.get("hash"):
        return JSONResponse(status_code=409, content={
            "detail": "Only these file types are explained: " + ", ".join(sorted(analysis.types)) + "."})
    analysis.queue(artifact, force=True)
    return {"message": "Queued for an AI explanation.", **analysis.analysis_for(artifact)}
