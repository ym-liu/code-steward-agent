from fastapi import APIRouter

from Services.ServiceContainer import services

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
def get_config():
    return services.config_service.get_config()


@router.post("/reload")
def reload_config():
    return services.config_service.reload()
