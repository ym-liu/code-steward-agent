from fastapi import APIRouter
from fastapi.responses import JSONResponse

from Services.ServiceContainer import services

router = APIRouter(prefix="/config", tags=["config"])


@router.get("")
def get_config():
    """Current settings with defaults filled in. Passwords, tokens and keys are shown as ***."""
    return services.config_service.public_config()


@router.post("/reload", responses={422: {"description": "The file is invalid; the previous settings stay in use."}})
def reload_config():
    """Read config.yaml again. Watched folders, logging and waiting rules change at once."""
    try:
        return services.config_service.reload()
    except Exception as error:
        return JSONResponse(status_code=422, content={
            "detail": f"Configuration was not reloaded: {error} The previous settings are still in use."})
