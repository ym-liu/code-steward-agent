from fastapi import APIRouter

from Services.ServiceContainer import services

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("")
def get_logs(limit: int = 100):
    return services.logs_service.list_logs(limit=limit)
