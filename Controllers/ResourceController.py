"""Read-only resource policy and live CPU admission state."""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse


router = APIRouter(prefix="/system", tags=["resources"])


def _governor():
    from Services.ServiceContainer import services
    return services.resource_governor


@router.get("/resources")
def resource_status(governor=Depends(_governor)):
    """Live system CPU, verified low process priority, pacing and AI cancellations.

    Reading this endpoint never starts a task or alters the policy. Unknown or
    stale measurements pause heavy work while this status API remains available.
    """
    return JSONResponse(governor.snapshot(), headers={"Cache-Control": "no-store"})
