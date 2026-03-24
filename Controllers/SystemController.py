from fastapi import APIRouter

from Services.ServiceContainer import services

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def get_health():
    return services.system_service.get_health()


@router.get("/status")
def get_status():
    return services.system_service.get_status()
