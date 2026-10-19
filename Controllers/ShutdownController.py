"""Local farewell page and explicit, guarded shutdown controls."""
import ipaddress
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


router = APIRouter(prefix="/shutdown", tags=["shutdown"])
ASSET_ROOT = Path(__file__).resolve().parents[1] / "static" / "shutdown"
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def shutdown_page():
    return FileResponse(ASSET_ROOT / "index.html", media_type="text/html", headers=HEADERS)


@router.get("/assets/shutdown.css", include_in_schema=False)
def shutdown_css():
    return FileResponse(ASSET_ROOT / "shutdown.css", media_type="text/css", headers=HEADERS)


@router.get("/assets/shutdown.js", include_in_schema=False)
def shutdown_js():
    return FileResponse(ASSET_ROOT / "shutdown.js", media_type="text/javascript", headers=HEADERS)


@router.get("/assets/farewell.png", include_in_schema=False)
def farewell_image():
    return FileResponse(ASSET_ROOT / "farewell.png", media_type="image/png", headers=HEADERS)


@router.get("/assets/clockoff-turnstile.webp", include_in_schema=False)
def clockoff_webp():
    return FileResponse(ASSET_ROOT / "clockoff-turnstile.webp", media_type="image/webp", headers=HEADERS)


@router.get("/assets/clockoff-turnstile.gif", include_in_schema=False)
def clockoff_gif():
    return FileResponse(ASSET_ROOT / "clockoff-turnstile.gif", media_type="image/gif", headers=HEADERS)


@router.get("/assets/clockoff-poster.png", include_in_schema=False)
def clockoff_poster():
    return FileResponse(ASSET_ROOT / "clockoff-poster.png", media_type="image/png", headers=HEADERS)


@router.get("/assets/clockoff-badge-turnstile.webp", include_in_schema=False)
def clockoff_badge_webp():
    return FileResponse(ASSET_ROOT / "clockoff-badge-turnstile.webp", media_type="image/webp", headers=HEADERS)


@router.get("/assets/clockoff-badge-turnstile.gif", include_in_schema=False)
def clockoff_badge_gif():
    return FileResponse(ASSET_ROOT / "clockoff-badge-turnstile.gif", media_type="image/gif", headers=HEADERS)


@router.get("/assets/clockoff-badge-poster.png", include_in_schema=False)
def clockoff_badge_poster():
    return FileResponse(ASSET_ROOT / "clockoff-badge-poster.png", media_type="image/png", headers=HEADERS)


def _coordinator(request):
    return getattr(request.app.state, "shutdown_coordinator", None)


@router.get("/status")
def shutdown_status(request: Request):
    coordinator = _coordinator(request)
    if coordinator is None:
        return JSONResponse({"supported": False, "token": None, "state": "idle", "checks": [],
                             "can_finish": False,
                             "detail": "This launch method does not support shutdown from the browser. Press Ctrl+C in the terminal, then run venv\\Scripts\\python launch.py."},
                            headers=HEADERS)
    return JSONResponse(coordinator.snapshot(), headers=HEADERS)


class ShutdownRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


def _authorized(request, token):
    try:
        if request.client is None or not ipaddress.ip_address(request.client.host).is_loopback:
            raise ValueError("non-local client")
        host = request.url.hostname
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError("non-local host")
        if request.headers.get("X-Code-Steward-Action") != "shutdown":
            raise ValueError("missing action header")
        origin = request.headers.get("origin")
        if origin is not None:
            parsed = urlsplit(origin)
            if (parsed.scheme != request.url.scheme or parsed.netloc.lower() != request.url.netloc.lower()
                    or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
                raise ValueError("foreign origin")
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            raise ValueError("cross-origin request")
    except (ValueError, TypeError):
        raise HTTPException(status_code=403, detail="Shutdown requires an explicit request from a same-origin page on this computer.") from None
    coordinator = _coordinator(request)
    if coordinator is None:
        raise HTTPException(status_code=503, detail="This launch method does not support shutdown from the browser. Start the application with launch.py.")
    if not token.isascii() or not secrets.compare_digest(token, coordinator.token):
        raise HTTPException(status_code=403, detail="The shutdown token is no longer valid. Refresh the page, then retry.")
    return coordinator


@router.post("/start", status_code=202)
def start_shutdown(payload: ShutdownRequest, request: Request):
    coordinator = _authorized(request, payload.token)
    try:
        snapshot = coordinator.start()
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return JSONResponse(snapshot, status_code=202, headers=HEADERS)


@router.post("/finish", status_code=202)
def finish_shutdown(payload: ShutdownRequest, request: Request):
    coordinator = _authorized(request, payload.token)
    try:
        coordinator.accept_finish()
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    # Starlette sends the response body before running its background task.
    return JSONResponse({"accepted": True}, status_code=202, headers=HEADERS,
                        background=BackgroundTask(coordinator.commit))
