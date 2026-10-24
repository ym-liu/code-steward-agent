import time
import signal
import logging
import threading
from datetime import datetime, timezone
from enum import Enum

from Services.ResourceGovernor import ResourceCancelled


#SERVICE STATE

class ServiceState(Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"

#LOGGER SETUP
# The log file is set up by ServiceContainer from config.yaml (storage.log_path, logging.*).

logger = logging.getLogger("code-steward.service")

#CONTROL METHODS

class CodeStewardService:
    """The background agent: a loop that turns waiting file changes into scans.

    Each tick it takes the changes that have settled in the event aggregator and checks them,
    and every scan_interval_seconds it runs a full scan to catch anything the watcher missed.
    A full scan also runs at start, after resume when changes overflowed, and on request.
    While paused, changes keep waiting and nothing is lost; after a restart the start-up scan
    finds whatever changed while the agent was off.
    """

    TICK_INTERVAL_SECONDS = 5
    MAX_BACKOFF_SECONDS = 300

    def __init__(self, file_event_service=None, scan_service=None, aggregator=None, analysis_service=None):
        self.state = ServiceState.STOPPED
        self.started_at = None
        self.tick_count = 0
        self.file_event_service = file_event_service
        self.scan_service = scan_service
        self.aggregator = aggregator
        self.analysis_service = analysis_service  # AI explanations run in their own thread while this service runs
        self.last_error = None
        self._failures = 0
        self._next_full_scan = 0.0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None

    def start(self):
        with self._lock:
            if self.state != ServiceState.STOPPED:
                logger.warning("Service is already running.")
                return False
            logger.info("service starting...")
            self.state = ServiceState.RUNNING
            self.started_at = datetime.now(timezone.utc)
            self.tick_count = 0
            self._failures = 0
        if self.file_event_service is not None:
            self.file_event_service.start()  # logs problems; periodic scans still run without it
        self.request_full_scan("startup-scan")
        if self.analysis_service is not None:
            self.analysis_service.start()
        self._thread = threading.Thread(target=self.run_loop, name="code-steward-loop", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        with self._lock:
            if self.state in (ServiceState.STOPPED, ServiceState.STOPPING):
                logger.warning("Service is already stopped.")
                return False
            logger.info("Service stopping...")
            self.state = ServiceState.STOPPING
        self._wake.set()
        return True

    def wait_stopped(self, timeout=None):
        """Wait until the loop has finished its current step and shut down (graceful shutdown)."""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return self.state == ServiceState.STOPPED

    def pause(self):
        with self._lock:
            if self.state != ServiceState.RUNNING:
                logger.warning("Service is not running")
                return False
            logger.info("Service paused; file changes keep waiting until resume.")
            self.state = ServiceState.PAUSED
        return True

    def resume(self):
        with self._lock:
            if self.state != ServiceState.PAUSED:
                logger.warning("Service is not paused.")
                return False
            logger.info("Service resumed")
            self.state = ServiceState.RUNNING
        self._wake.set()
        if self.analysis_service is not None:
            self.analysis_service.wake()
        return True

    def request_full_scan(self, reason="manual-scan"):
        """Queue a full scan; the loop runs it at the next tick."""
        logger.log(logging.INFO if reason == "manual-scan" else logging.DEBUG, "Full scan requested (%s).", reason)
        if self.aggregator is not None:
            self.aggregator.request_full_scan(reason)
            self._wake.set()

    def tick_seconds(self):
        if self.scan_service is not None and self.scan_service.config_service is not None:
            return self.scan_service.config_service.get("service", "tick_seconds", default=self.TICK_INTERVAL_SECONDS)
        return self.TICK_INTERVAL_SECONDS

    def get_status(self) -> dict:
        return{
            "state": self.state.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "tick_count": self.tick_count,
            "uptime_seconds": (
                (datetime.now(timezone.utc) - self.started_at).total_seconds()
                if self.started_at else 0
            ),
            "waiting_changes": self.aggregator.status() if self.aggregator is not None else None,
            "file_watcher": self.file_event_service.status() if self.file_event_service is not None else None,
            "last_error": self.last_error,
        }

#INTERNAL LOOP

    def run_loop(self):
        logger.info("Service loop started (tick interval: %ss)", self.tick_seconds())

        while self.state != ServiceState.STOPPING:

            if self.state == ServiceState.PAUSED:
                self._wake.wait(self.tick_seconds())
                self._wake.clear()
                continue
            self.tick_count += 1
            delay = self.tick_seconds()
            try:
                self.do_work()
                self._failures = 0
            except ResourceCancelled:
                # Keep a complete rescan pending if shutdown cancelled a partial scan.
                if self.aggregator is not None:
                    self.aggregator.request_full_scan("resource-resume-scan")
            except Exception as error:
                # Keep running: storage or disk problems are retried later, with growing pauses.
                self._failures += 1
                self.last_error = f"{type(error).__name__}: {error}"
                delay = min(delay * 2 ** self._failures, self.MAX_BACKOFF_SECONDS)
                logger.exception("Background work failed (%d in a row); retrying in %.0f s.", self._failures, delay)
                if self.aggregator is not None:
                    self.aggregator.request_full_scan("retry-scan")  # re-check the changes taken in this step
            self._wake.wait(delay)
            self._wake.clear()

        self.on_shutdown()


    def do_work(self):
        """Check settled file changes, and run a full scan when one is due or requested."""
        logger.debug("Tick #%d - service running.", self.tick_count)
        if self.scan_service is None or self.aggregator is None:
            return
        governor = getattr(self, "resource_governor", None)
        if governor is not None and not governor.can_work():
            return  # Do not consume queued events while CPU resources are unavailable.
        if self.file_event_service is not None and self.file_event_service.refresh():
            self.aggregator.request_full_scan("watch-scan")  # a root folder came back or went away
        now = time.monotonic()
        files, folders, reason = self.aggregator.take_due()
        if reason is None and now >= self._next_full_scan and self._next_full_scan:
            reason = "periodic-scan"
        if reason is not None:
            interval = self.scan_service.config_service.get("scanning", "scan_interval_seconds", default=300) \
                if self.scan_service.config_service is not None else 300
            self._next_full_scan = now + interval
        if files or folders or reason:
            self.scan_service.process(files=files, folders=folders, full_scan=reason)
        self.last_error = None

    def on_shutdown(self):
        logger.info(
            "Service shutting down after %d ticks (uptime: %.1fs)",
            self.tick_count,
            (datetime.now(timezone.utc) - self.started_at).total_seconds() if self.started_at else 0,
        )
        if self.file_event_service is not None:
            self.file_event_service.stop()
        if self.analysis_service is not None:
            self.analysis_service.stop(timeout=5)  # a file still being explained is explained again next start
        if self.aggregator is not None:
            self.aggregator.clear()  # the start-up scan finds these changes next time
        self._next_full_scan = 0.0
        self.state = ServiceState.STOPPED
        self.started_at = None

#SIGNAL HANDLERS

def attach_signal_handlers(service):
    def handle(sig, frame):
        sig_name = signal.Signals(sig).name
        logger.info("Received signal %s requesting shutdown", sig_name)
        service.stop()

    try:
        signal.signal(signal.SIGINT,  handle)
        signal.signal(signal.SIGTERM, handle)
    except ValueError:
        logger.debug("Signal handlers can only be set in the main thread; skipped.")
        return
    logger.info("Signal handlers registered (SIGINT, SIGTERM)")
