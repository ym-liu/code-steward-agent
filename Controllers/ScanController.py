import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from Services.ServiceContainer import services

logger = logging.getLogger("code-steward.scanner")
router = APIRouter(prefix="/scan", tags=["scan"])


@router.get("/status")
def get_scan_status():
    """Whether a scan is running, the last scan's counts, the last full scan time and root availability."""
    return services.scan_service.get_status()


@router.post("/trigger", status_code=202,
             responses={409: {"description": "The service is stopped or paused; the scan was not queued."}})
def trigger_scan():
    """Queue a full scan of every root. It runs in the background within one tick.

    Follow it with GET /scan/status, then GET /artifacts.
    """
    state = services.code_steward_service.state.value
    if state != "running":
        logger.warning("Scan blocked: service is %s; start or resume it before scanning.", state)
        return JSONResponse(status_code=409, content={
            "detail": f"Service is {state}. Start or resume it before scanning.", "state": state})
    services.code_steward_service.request_full_scan("manual-scan")
    return {"message": "Full scan queued.", "state": state}
