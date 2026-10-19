"""Additive startup UI and read-only readiness checks; never starts background work."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(prefix="/startup", tags=["startup"])
ASSET_ROOT = Path(__file__).resolve().parents[1] / "static" / "startup"
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
PROBE_TIMEOUT = 4


def _services():
    # Keep importing this router independent from the application's global container.
    from Services.ServiceContainer import services
    return services


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def startup_page():
    return FileResponse(ASSET_ROOT / "index.html", media_type="text/html", headers=HEADERS)


@router.get("/assets/startup.css", include_in_schema=False)
def startup_css():
    return FileResponse(ASSET_ROOT / "startup.css", media_type="text/css", headers=HEADERS)


@router.get("/assets/startup.js", include_in_schema=False)
def startup_js():
    return FileResponse(ASSET_ROOT / "startup.js", media_type="text/javascript", headers=HEADERS)


def _check(identifier, label, state, detail, action=None):
    result = {"id": identifier, "label": label, "state": state, "detail": detail}
    if action:
        result["action"] = action
    return result


def _guard(identifier, label, probe):
    try:
        return probe()
    except Exception as error:
        return _check(identifier, label, "error", f"Check failed: {error}", "See /logs for details, resolve the issue, then retry.")


def _service_check(services):
    service = services.code_steward_service
    status = service.get_status()
    if status.get("last_error"):
        return _check("service", "Background service", "error", f"Background task failed: {status['last_error']}", "See /logs, resolve the error, then retry.")
    state = status.get("state")
    if state != "running":
        actions = {"paused": "Call POST /system/resume in /docs, then retry.", "stopped": "Call POST /system/start in /docs, then retry."}
        return _check("service", "Background service", "error", f"Background service status: {state or 'unknown'}.", actions.get(state, "Wait for the service to stop, start it again, then retry."))
    thread = getattr(service, "_thread", None)
    if thread is not None and not thread.is_alive():
        return _check("service", "Background service", "error", "The background thread has stopped.", "See /logs and restart the background service.")
    if thread is None or status.get("tick_count", 0) < 1:
        return _check("service", "Background service", "pending", "The background service has started; waiting for its first work cycle.")
    return _check("service", "Background service", "ready", f"The background thread is running; completed {status['tick_count']} work cycles.")


def _storage_check(services):
    # mode=ro must not create a new database or repair/alter the application's data.
    path = Path(services.artifacts_service.database_path).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
        count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    return _check("storage", "File index", "ready", f"The index database is readable. File records: {count}.")


def _roots_problem(services, roots):
    if not roots:
        return "No scan directories are configured for this operating system."
    missing = [item["path"] for item in roots if not item.get("available")]
    if missing:
        return "Scan directories are unavailable: " + "; ".join(missing)
    scan = services.scan_service
    excluded = [item["path"] for item in roots if scan.is_denied(item["path"]) or scan.in_library(item["path"])]
    if excluded:
        return "Scan directories are excluded by protection rules or are inside the organization library: " + "; ".join(excluded)
    return None


def _watcher_check(services):
    watcher = services.file_event_service
    problem = _roots_problem(services, services.scan_service.get_status().get("roots", []))
    if problem:
        return _check("watcher", "File monitoring", "error", problem, "Check the directories and the scanning.root_paths and deny_paths settings, then retry.")
    status = watcher.status()
    if status.get("missing"):
        return _check("watcher", "File monitoring", "error", "Some monitored directories are unavailable: " + "; ".join(status["missing"]), "Restore the directories or update the scan settings, then retry.")
    observer = getattr(watcher, "observer", None)
    if not status.get("running") or not status.get("watched") or observer is None or not observer.is_alive():
        return _check("watcher", "File monitoring", "error", "File monitoring is not running, or the monitoring thread has stopped.", "See /logs, check directory permissions and monitoring settings, then retry.")
    emitters = list(observer.emitters)
    if not emitters or any(not emitter.is_alive() for emitter in emitters):
        return _check("watcher", "File monitoring", "error", "One or more file monitoring worker threads are not running.", "See /logs, restore the directories or restart the background service, then retry.")
    mode = "polling" if status.get("mode") == "polling" else "system events"
    return _check("watcher", "File monitoring", "ready", f"The monitoring thread is alive; using {mode} to watch {len(status['watched'])} directories.")


def _timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _scan_check(services):
    status = services.scan_service.get_status()
    problem = _roots_problem(services, status.get("roots", []))
    if problem:
        return _check("scan", "Startup scan", "error", problem, "Restore the directories or correct the scan settings, then retry.")
    service = services.code_steward_service.get_status()
    if service.get("last_error"):
        return _check("scan", "Startup scan", "error", f"The scan task reported an error: {service['last_error']}", "See /logs, resolve the error, then retry.")
    pending = (service.get("waiting_changes") or {}).get("full_scan_requested")
    if status.get("is_scanning") or pending:
        return _check("scan", "Startup scan", "pending", "Scanning directories." if status.get("is_scanning") else "A full scan is queued for background processing.")
    started = _timestamp(service.get("started_at"))
    finished = _timestamp(status.get("last_full_scan_at"))
    if started is None or finished is None or finished < started:
        return _check("scan", "Startup scan", "pending", "Waiting for a full scan to finish after this service startup.")
    errors = (status.get("last_summary") or {}).get("errors", 0)
    if errors:
        return _check("scan", "Startup scan", "error", f"The latest scan reported {errors} file read errors.", "See /logs, check file permissions or disk status, resolve the issue, then scan again.")
    return _check("scan", "Startup scan", "ready", "The full scan for this startup is complete; the latest scan reported no file read errors.")


def readiness(services):
    checks = [_check("api", "Local API", "ready", "The local API has responded; background components are being checked individually.")]
    for identifier, label, probe in (("service", "Background service", _service_check), ("storage", "File index", _storage_check),
                                      ("watcher", "File monitoring", _watcher_check), ("scan", "Startup scan", _scan_check)):
        checks.append(_guard(identifier, label, lambda probe=probe: probe(services)))
    return {"checks": checks, "core_ready": all(item["state"] == "ready" for item in checks)}


@router.get("/status")
def startup_status(services=Depends(_services)):
    return JSONResponse(readiness(services), headers=HEADERS)


def _ollama_client(settings):
    from ai.explain.ollama_client import OllamaClient

    class StartupProbeClient(OllamaClient):
        def request(self, path, data=None, timeout=None, on_chunk=None):
            return super().request(path, data=data, timeout=min(timeout or PROBE_TIMEOUT, PROBE_TIMEOUT), on_chunk=on_chunk)

    return StartupProbeClient(dict(settings))


def ai_readiness(services):
    try:
        configured = services.config_service.get_config()["ai"]
        if configured["enabled"] is False:
            return _check("ai", "Local AI", "skipped", "AI is disabled in the settings, so this check is skipped. File scanning and monitoring remain available.")
        analysis = services.analysis_service
        if not analysis.enabled or not analysis.ai_settings or not analysis.tag:
            return _check("ai", "Local AI", "error", f"AI settings could not be loaded: {analysis.state}", "Check ai.settings_file and ai.model, resolve the issue, then reload the configuration.")
        client = _ollama_client(analysis.ai_settings)
        tag = analysis.tag
        installed = client.match(client.installed(), tag)
        if installed is None:
            return _check("ai", "Local AI", "error", f"The configured model is not installed in Ollama: {tag}.", f"Run ollama pull {tag} manually, then retry after it finishes.")
        info = client.request("/api/show", {"model": tag})
        if info.get("remote_host") or info.get("remote_model"):
            return _check("ai", "Local AI", "error", "The current model depends on the cloud and cannot be used for local AI.", "Set ai.model to an installed local model, then reload the configuration.")
        own = set(getattr(analysis, "_own_models", ())) | {tag}
        others = [model.get("name", model.get("model", "unknown model")) for model in client.running()
                  if not any(client.match([model], allowed) for allowed in own)]
        if others:
            return _check("ai", "Local AI", "error", "Other models are in use in Ollama: " + "; ".join(others), "Stop the models used by other applications or wait for them to finish, then retry.")
        return _check("ai", "Local AI", "ready", f"Ollama is reachable and the local model is installed; the model loads on the first inference ({tag}).")
    except Exception as error:
        code = getattr(error, "code", "")
        actions = {
            "connection_error": "Open Ollama or run ollama serve, then retry.",
            "timeout": "Ollama did not respond within 4 seconds. Check its status, then retry.",
            "config_error": "Check the local AI settings and base_url, then reload the configuration.",
            "response_error": "Ollama returned invalid data. Check Ollama, then retry.",
        }
        return _check("ai", "Local AI", "error", f"Local AI check failed: {error}", actions.get(code, "Check Ollama and the model settings, see /logs, then retry."))


@router.get("/ai")
def startup_ai(services=Depends(_services)):
    return JSONResponse(ai_readiness(services), headers=HEADERS)


# One app-level UI registration keeps the original entrypoint integration at two lines.
from Controllers.ShutdownController import router as shutdown_router

startup_routes = router
router = APIRouter()
router.include_router(startup_routes)
router.include_router(shutdown_router)
