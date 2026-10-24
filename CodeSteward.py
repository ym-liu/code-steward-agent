import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from Controllers.AnalysisController import router as analysis_router
from Controllers.ArtifactsController import router as artifacts_router
from Controllers.ConfigController import router as config_router
from Controllers.EventsController import router as events_router
from Controllers.LogsController import router as logs_router
from Controllers.OrganizationController import router as organization_router
from Controllers.ScanController import router as scan_router
from Controllers.SystemController import router as system_router
from Controllers.StartupController import router as startup_router
from Controllers.ResourceController import router as resource_router
from Services.ServiceContainer import services

logger = logging.getLogger("code-steward.api")


@asynccontextmanager
async def lifespan(app):
    """Start the background service with the API (service.auto_start) and stop it cleanly at shutdown."""
    if services.config_service.get("service", "auto_start", default=True):
        services.code_steward_service.start()
    yield
    services.resource_governor.begin_shutdown()
    if not await run_in_threadpool(services.execution_service.wait, 30):
        logger.warning("An organization operation is still running; its journal will be checked at next start.")
    if services.code_steward_service.state.value != "stopped":
        services.code_steward_service.stop()
        await run_in_threadpool(services.code_steward_service.wait_stopped, 30)
    if not await run_in_threadpool(services.organization_service.wait, 10):
        logger.info("The organization plan being built was not saved; build it again after the next start.")
    services.resource_governor.stop()


app = FastAPI(
    title="Code Steward Agent API",
    description=(
        "Local-first code steward API for system health, scan orchestration, "
        "artifact discovery, AI explanations, organization plans, logs, and configuration controls."
    ),
    lifespan=lifespan,
)

app.include_router(system_router)
app.include_router(scan_router)
app.include_router(artifacts_router)
app.include_router(analysis_router)
app.include_router(organization_router)
app.include_router(events_router)
app.include_router(logs_router)
app.include_router(config_router)
app.include_router(startup_router)
app.include_router(resource_router)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    # Every response stays JSON; the details go to the log.
    logger.exception("Unexpected error while handling %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Unexpected server error. GET /logs shows the details."})


@app.get("/")
def read_root():
    return {
        "project": "code-steward-agent",
        "status": "running",
        "docs": "/docs",
    }
