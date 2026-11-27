"""Gracefully drain this application; never stop, unload or reconfigure Ollama."""
import logging
import secrets
import threading
import time

from starlette.responses import JSONResponse


logger = logging.getLogger("code-steward.shutdown")
MUTATING_EXCEPTIONS = {"/shutdown/start", "/shutdown/finish"}


class ShutdownCoordinator:
    """Coordinate existing public lifecycle methods without changing their implementation.

    The shared organization lock is retained while the farewell page is visible. A
    short grace timer completes shutdown if the browser disappears after cleanup.
    """

    def __init__(self, services, request_exit, *, poll_seconds=0.25, grace_seconds=15):
        self.services = services
        self.request_exit = request_exit
        self.token = secrets.token_urlsafe(32)
        self.poll_seconds = poll_seconds
        self.grace_seconds = grace_seconds
        self._condition = threading.Condition(threading.RLock())
        self._mutations = 0
        self._closing = False
        self._state = "idle"
        self._error = None
        self._worker = None
        self._timer = None
        self._barrier_held = False
        self._exit_requested = False
        self._began = None
        self._checks = self._new_checks()

    @staticmethod
    def _new_checks():
        return [{"id": key, "label": label, "state": "pending", "detail": "Shutdown has not started."}
                for key, label in (("operations", "Current operations"), ("service", "Scanning and file monitoring"),
                                   ("ai", "Local AI analysis tasks"))]

    def snapshot(self):
        with self._condition:
            checks = [dict(item) for item in self._checks]
            if self._began is not None and time.monotonic() - self._began >= 30:
                for check in checks:
                    if check["state"] == "pending" and self._state == "closing":
                        check["action"] = "This has taken more than 30 seconds; waiting for the current task to finish safely. See /logs and keep the application running."
            result = {"supported": True, "token": self.token, "state": self._state,
                      "checks": checks, "can_finish": self._state == "ready"}
            if self._error:
                result["error"] = self._error
            return result

    def admit_mutation(self):
        """Admission and closing share a lock, so a request cannot pass unnoticed."""
        with self._condition:
            if self._closing:
                return False
            self._mutations += 1
            return True

    def mutation_finished(self):
        with self._condition:
            self._mutations -= 1
            self._condition.notify_all()

    def start(self):
        with self._condition:
            if self._state in {"closing", "ready", "exiting"}:
                return self.snapshot()
            # A failure can be retried only after its worker has really returned.
            if self._worker is not None and self._worker.is_alive():
                raise RuntimeError("The previous shutdown check is still finishing. Please retry shortly.")
            self._closing = True
            self._state, self._error = "closing", None
            self._began = time.monotonic()
            self._checks = self._new_checks()
            self._worker = threading.Thread(target=self._drain, name="code-steward-shutdown", daemon=True)
            self._worker.start()
            return self.snapshot()

    def _check(self, identifier, state, detail):
        with self._condition:
            for check in self._checks:
                if check["id"] == identifier:
                    check.update(state=state, detail=detail)
                    break

    def _fail(self, identifier, error):
        detail = f"{type(error).__name__}: {error}"
        with self._condition:
            self._state, self._error = "error", detail
            self._check(identifier, "error", detail)
        logger.exception("Graceful shutdown failed during %s", identifier)

    def _drain(self):
        stage = "operations"
        try:
            governor = getattr(self.services, "resource_governor", None)
            if governor is not None:
                governor.begin_shutdown()
            logger.info("Graceful shutdown requested; waiting for current operations.")
            self._check(stage, "pending", "Waiting for accepted operations to finish, including copying, undoing and organization planning.")
            with self._condition:
                while self._mutations:
                    self._condition.wait(self.poll_seconds)
            operation_lock = self.services.organization_service.operation_lock
            while not self._barrier_held:
                if operation_lock.acquire(timeout=self.poll_seconds):
                    self._barrier_held = True
            # The operation lock also covers synchronous preflight/repair calls;
            # join workers after taking it because release precedes thread return.
            while not self.services.execution_service.wait(self.poll_seconds):
                pass
            while not self.services.organization_service.wait(self.poll_seconds):
                pass
            self._check(stage, "ready", "All accepted operations have finished; new write operations are blocked.")

            stage = "service"
            self._check(stage, "pending", "Waiting for the current scan to finish and stopping file monitoring.")
            service = self.services.code_steward_service
            if service.state.value != "stopped":
                service.stop()
            while not service.wait_stopped(self.poll_seconds):
                thread = getattr(service, "_thread", None)
                if thread is not None and not thread.is_alive():
                    raise RuntimeError("The background thread exited without reporting a safe stop. See /logs.")
            thread = getattr(service, "_thread", None)
            if thread is not None and thread.is_alive():
                raise RuntimeError("The background thread has not exited yet.")
            watcher = self.services.file_event_service
            observer = getattr(watcher, "observer", None)
            if watcher.status().get("running") or (observer is not None and observer.is_alive()):
                raise RuntimeError("File monitoring is still running. The API will remain open.")
            if observer is not None and any(item.is_alive() for item in list(observer.emitters)):
                raise RuntimeError("File monitoring worker threads are still running. The API will remain open.")
            if self.services.scan_service.get_status().get("is_scanning"):
                raise RuntimeError("A scan task is still running. The API will remain open.")
            self._check(stage, "ready", "The scan thread and file monitoring have stopped.")

            stage = "ai"
            self._check(stage, "pending", "Waiting for this application's AI request to finish or yield safely; Ollama will keep running.")
            # CodeStewardService.stop waits only 5 seconds for analysis. Its STOPPED
            # state alone is insufficient evidence that an inference has finished.
            while not self.services.analysis_service.stop(timeout=self.poll_seconds):
                current = getattr(self.services.analysis_service, "current", None)
                self._check(stage, "pending", "Waiting for this application's AI request to finish or yield safely; Ollama will keep running."
                            + (f" Current file: {current}" if current else ""))
            self._check(stage, "ready", "The AI analysis thread has stopped; completed results are saved and interrupted jobs remain queued. Ollama is still running.")
            with self._condition:
                self._state = "ready"
                self._timer = threading.Timer(self.grace_seconds, self.commit)
                self._timer.daemon = True
                self._timer.start()
            logger.info("Background work stopped safely; ready to close the API.")
        except Exception as error:
            self._fail(stage, error)

    def accept_finish(self):
        """Reserve exit now; actual server exit is invoked after the HTTP response."""
        with self._condition:
            if self._state == "exiting":
                return
            if self._state != "ready":
                raise RuntimeError("Some background tasks have not stopped safely yet. The API cannot close.")
            self._state = "exiting"
            if self._timer is not None:
                self._timer.cancel()

    def commit(self):
        with self._condition:
            if self._state not in {"ready", "exiting"} or self._exit_requested:
                return
            self._state = "exiting"
            self._exit_requested = True
            if self._timer is not None:
                self._timer.cancel()
        try:
            self.request_exit()
        except Exception as error:
            with self._condition:
                self._exit_requested = False
            self._fail("service", error)
            return
        if self._barrier_held:
            self.services.organization_service.operation_lock.release()
            self._barrier_held = False
        logger.info("Graceful API exit requested.")


class ShutdownAdmissionMiddleware:
    """Count admitted mutations and reject new ones while draining, including on error."""

    def __init__(self, app, coordinator):
        self.app, self.coordinator = app, coordinator

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}
                or scope.get("path") in MUTATING_EXCEPTIONS):
            await self.app(scope, receive, send)
            return
        if not self.coordinator.admit_mutation():
            response = JSONResponse(status_code=503, content={"detail": "The application is shutting down safely and is no longer accepting new operations."},
                                    headers={"Cache-Control": "no-store"})
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.coordinator.mutation_finished()
