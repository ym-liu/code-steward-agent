from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from Services.ServiceContainer import services

router = APIRouter(prefix="/events", tags=["events"])


class InjectedEvent(BaseModel):
    event_type: Literal["created", "modified", "deleted", "moved"]
    path: str = Field(description="File or folder path; it must be inside a configured root.")
    dest_path: str | None = Field(default=None, description="The new path, for moved events.")
    is_directory: bool = Field(default=False, description="True when path is a folder.")


@router.get("/status")
def get_event_status():
    """File changes waiting to be checked, and how the folders are being watched."""
    return {"waiting": services.aggregator.status(), "watcher": services.file_event_service.status()}


@router.post("/inject", status_code=202, responses={403: {"description": "Event injection is turned off."}})
def inject_event(event: InjectedEvent):
    """Simulate a file-system event for testing. It follows the same filters and waiting rules as a
    real event. Turn it on with testing.allow_event_injection: true in config.yaml, then reload."""
    if not services.config_service.get("testing", "allow_event_injection", default=False):
        raise HTTPException(status_code=403, detail="Event injection is off. Set testing.allow_event_injection: "
                                                    "true in config.yaml and call POST /config/reload.")
    queued = services.file_event_service.event_handler.handle(
        event.event_type, event.path, event.dest_path or "", event.is_directory, source="injected")
    result = {"queued": queued, "waiting": services.aggregator.status()}
    if not queued:
        result["note"] = "Ignored: the path is outside the configured folders, excluded or a temporary file."
    return result
