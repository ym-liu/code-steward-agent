from fastapi import APIRouter
from fastapi.responses import JSONResponse

from Services.ServiceContainer import services

router = APIRouter(prefix="/system", tags=["system"])
REFUSED = {409: {"description": "The service is not in a state that allows this; the body says why."}}


def _control(result):
    """200 when the state changed; 409 when the service was already in another state."""
    if result["changed"]:
        return {"message": result["message"], "state": result["state"]}
    return JSONResponse(status_code=409, content={"detail": result["message"], "state": result["state"]})


@router.get("/health")
def get_health():
    return services.system_service.get_health()

@router.get("/status")
def get_status():
    """Service state, waiting file changes, file watcher state and the last background error."""
    return services.system_service.get_status()

@router.get("/metrics")
def get_metrics():
    """CPU time, CPU share since the previous call, peak memory and threads of the agent process."""
    return services.system_service.get_metrics()

@router.post("/start", responses=REFUSED)
def start():
    return _control(services.system_service.start())

@router.post("/stop", responses=REFUSED)
def stop():
    return _control(services.system_service.stop())

@router.post("/pause", responses=REFUSED)
def pause():
    """Pause processing. File changes keep waiting and are handled after resume."""
    return _control(services.system_service.pause())

@router.post("/resume", responses=REFUSED)
def resume():
    return _control(services.system_service.resume())
