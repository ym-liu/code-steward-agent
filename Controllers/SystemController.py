from fastapi import APIRouter
from Services.ServiceContainer import services

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def get_health():
    return services.system_service.get_health()

@router.get("/status")
def get_status():
    return services.system_service.get_status()

@router.post("/start")
def start():
    return services.system_service.start()

@router.post("/stop")
def stop():
    return services.system_service.stop()

@router.post("/pause")
def pause():
    return services.system_service.pause()

@router.post("/resume")
def resume():
    return services.system_service.resume()