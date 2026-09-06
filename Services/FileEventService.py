import time
import threading
import logging
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger("code-steward.file-events")

class ScanTriggerHandler(FileSystemEventHandler):
    """Collects file changes and triggers one scan after a quiet period."""
        
    def __init__(self, scan_service,debounce_seconds: float = 2.0):
        self.scan_service = scan_service
        self.debounce_seconds = debounce_seconds
        self._timer = None
        self._lock = threading.Lock()
        self._pending_events = []
        
    def on_any_event(self, event: FileSystemEvent) -> None:
        # Ignore directory-level events (e.g. a folder being touched);
        # we only care about actual file changes.
        if event.is_directory:
            return

        if event.event_type not in {"created", "modified", "moved", "deleted"}:
            return
 
        logger.info("File event detected: %s (%s)", event.src_path, event.event_type)
        self._schedule_scan(event)
 
    def _schedule_scan(self, event):
        with self._lock:
            self._pending_events.append({
                "event_type": event.event_type,
                "source_path": event.src_path,
                "destination_path": getattr(event, "dest_path", None),
            })
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._run_scan)
            self._timer.daemon = True
            self._timer.start()
 
    def _run_scan(self):
        logger.info("Debounce window elapsed, triggering scan.")
        try:
            with self._lock:
                events = self._pending_events
                self._pending_events = []
            self.scan_service.enqueue_manual_scan(events=events)
        except Exception:
            logger.exception("Scan triggered by file event failed.")

class FileEventService:
    def __init__(self, scan_service, target_directories, debounce_seconds: float = 2.0):
        
        self.target_directories = target_directories
        self.event_handler = ScanTriggerHandler(scan_service, debounce_seconds)
        self.observer = Observer()
        self._is_running = False
        
    def start(self):
        """Starts monitoring in the background."""
        if self._is_running:
            logger.warning("File event service is already running.")
            return
        
        if not self.target_directories:
            logger.warning("No target directories configured; file watcher will not start.")
            return

        # Create a fresh Observer instance each time start() is called
        self.observer = Observer()

        for directory in self.target_directories:
            self.observer.schedule(self.event_handler, directory, recursive=True)
            logger.info("Watching directory: %s", directory)
            
        self.observer.start()
        self._is_running = True
        logger.info("File event service started.")

    def stop(self):
        """Stops monitoring."""
        if not self._is_running:
            return

        if self.observer:
            self.observer.stop()
            self.observer.join()
            self._is_running = False
            logger.info("File event service stopped.")
