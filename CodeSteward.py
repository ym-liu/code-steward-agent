from fastapi import FastAPI

from Controllers.ArtifactsController import router as artifacts_router
from Controllers.ConfigController import router as config_router
from Controllers.LogsController import router as logs_router
from Controllers.ScanController import router as scan_router
from Controllers.SystemController import router as system_router

app = FastAPI(
    title="Code Steward Agent API",
    description=(
        "Local-first code steward API for system health, scan orchestration, "
        "artifact discovery, logs, and configuration controls."
    ),
)

app.include_router(system_router)
app.include_router(scan_router)
app.include_router(artifacts_router)
app.include_router(logs_router)
app.include_router(config_router)


@app.get("/")
def read_root():
    return {
        "project": "code-steward-agent",
        "status": "running",
        "docs": "/docs",
    }
