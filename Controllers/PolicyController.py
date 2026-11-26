from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from Services.ServiceContainer import services

router = APIRouter(prefix="/policy", tags=["policy"])


class SimulationRequest(BaseModel):
    decision: dict[str, Any] = Field(description="Proposed JSON decision or action to evaluate without side effects")


@router.get("")
def get_policy():
    """Active policy rules and the latest in-process decision audit record."""
    return {**services.policy_service.rules(), "last_decision": services.policy_service.last_decision}


@router.post("/simulate")
def simulate_policy(request: SimulationRequest):
    """Evaluate a proposed decision without writing files, calling a model, or changing policy state."""
    return services.policy_service.simulate(request.decision)
