import logging
import os

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

logger = logging.getLogger("code-steward.file-events")


class FileChangeHandler(FileSystemEventHandler):
    """Hands every relevant file-system event to the event aggregator as "check this path".

    The handler does no scanning itself, so events are never lost while a scan runs or the
    service is paused. Paths outside the roots, excluded folders and temporary files are ignored.
    """

    def __init__(self, aggregator, scan_service):
        self.aggregator = aggregator
        self.scan_service = scan_service

    def on_any_event(self, event):
        self.handle(event.event_type, event.src_path, getattr(event, "dest_path", "") or "", event.is_directory)

    def handle(self, event_type, path, dest_path="", is_directory=False, source="watcher"):
        """Queue one event. Also used by POST /events/inject. Returns True when something was queued."""
        if event_type not in {"created", "modified", "moved", "deleted"}:
            return False
        scan = self.scan_service
        path = scan.canonical(path)
        dest_path = scan.canonical(dest_path) if dest_path else ""
        logger.debug("File event: %s %s%s", event_type, path, " -> " + dest_path if dest_path else "")
        if is_directory:
            if event_type == "modified":
                return False  # a folder is "modified" whenever a file inside changes
            # Both sides of a folder move are checked; a folder moved out of the roots leaves its files gone.
            queued = False
            for folder in (path, dest_path):
                if folder and scan.in_scope(folder, folder=True):
                    self.aggregator.add(folder, source=source, folder=True)
                    queued = True
            return queued
        if event_type == "moved":
            if scan.in_scope(dest_path):
                # An editor that saves through a temporary file "moves" it onto the real name:
                # that is an edit of the destination, not a rename.
                moved_from = path if scan.in_scope(path) else None
                self.aggregator.add(dest_path, moved_from=moved_from, source=source)
                return True
            path_known = scan.in_scope(path) or scan.is_known(path)
            if path_known:
                self.aggregator.add(path, source=source)  # moved out of the watched folders
            return path_known
        if scan.in_scope(path) or scan.is_known(path):
            self.aggregator.add(path, source=source)
            return True
        return False


class FileEventService:
    """Watches the configured roots and passes changes to the event aggregator.

    watch_mode auto uses the operating system's change notifications and falls back to polling
    when they cannot start; "events" and "polling" force one way. Roots that do not exist yet
    (for example an unplugged drive) are skipped and watched as soon as they appear.
    """

    def __init__(self, aggregator, scan_service):
        self.scan_service = scan_service
        self.event_handler = FileChangeHandler(aggregator, scan_service)
        self.observer = None
        self.mode = None
        self.watched = []
        self.missing = []
        self._is_running = False

    def start(self):
        """Starts monitoring in the background. Never raises: problems are logged."""
        if self._is_running:
            logger.warning("File event service is already running.")
            return True
        roots = self.scan_service.roots()
        self.watched = [root for root in roots if os.path.isdir(root)]
        self.missing = [root for root in roots if root not in self.watched]
        for root in self.missing:
            logger.warning("Watched folder not found; it will be watched when it appears: %s", root)
        if not self.watched:
            logger.warning("No existing folders to watch; file changes are found by periodic scans only.")
            return False
        wanted = self.scan_service.setting("watch_mode", "auto")
        modes = {"auto": ["events", "polling"], "events": ["events"], "polling": ["polling"]}.get(wanted, ["events", "polling"])
        for mode in modes:
            observer = Observer() if mode == "events" else PollingObserver(
                timeout=self.scan_service.setting("polling_interval_seconds", 30))
            try:
                for directory in self.watched:
                    observer.schedule(self.event_handler, directory, recursive=True)
                observer.start()
            except Exception as error:
                logger.warning("Could not start %s watching (%s).", mode, error)
                continue
            self.observer, self.mode, self._is_running = observer, mode, True
            for directory in self.watched:
                logger.info("Watching directory: %s", directory)
            logger.info("File event service started (%s).", "system change events" if mode == "events" else "polling")
            return True
        logger.error("File watching could not start; file changes are found by periodic scans only.")
        return False

    def stop(self):
        """Stops monitoring."""
        if not self._is_running:
            return
        if self.observer:
            self.observer.stop()
            self.observer.join()
        self._is_running = False
        logger.info("File event service stopped.")

    def restart(self):
        """Watch the current roots again (after a settings change or when a folder appears)."""
        self.stop()
        return self.start()

    def refresh(self):
        """Start watching folders that were missing before, such as a drive plugged in later."""
        appeared = [root for root in self.missing if os.path.isdir(root)]
        if appeared:
            for root in appeared:
                logger.info("Folder is available again: %s", root)
            self.restart()
        return bool(appeared)

    def status(self):
        return {"running": self._is_running, "mode": self.mode, "watched": self.watched, "missing": self.missing}
