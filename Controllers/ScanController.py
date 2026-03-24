from fastapi import APIRouter

from Services.ServiceContainer import services

router = APIRouter(prefix="/scan", tags=["scan"])


@router.get("/status")
def get_scan_status():
    return services.scan_service.get_status()


@router.post("/trigger")
def trigger_scan():
    # Manual trigger is useful for tests and immediate re-index requests.
    return services.scan_service.enqueue_manual_scan()
