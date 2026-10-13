import logging
import os
import threading
import time

logger = logging.getLogger("code-steward.events")


class EventAggregator:
    """Turns bursts of file events into one "check this path" item per file.

    Watcher events and test injections arrive here. An item waits until its file has been quiet
    for quiet_seconds (but never longer than max_wait_seconds), so one editor save or one copied
    folder becomes a single piece of work. Nothing is dropped while the scanner is busy or the
    service is paused: items wait until the service loop takes them. When more than
    max_pending_files are waiting, they are replaced by one full scan.
    """

    def __init__(self, quiet_seconds=2.0, max_wait_seconds=30.0, max_pending_files=10000, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._files = {}
        self._folders = {}
        self._full_scan_reason = None
        self.configure(quiet_seconds, max_wait_seconds, max_pending_files)

    def configure(self, quiet_seconds, max_wait_seconds, max_pending_files):
        with self._lock:
            self.quiet_seconds = quiet_seconds
            self.max_wait_seconds = max_wait_seconds
            self.max_pending_files = max_pending_files

    def add(self, path, moved_from=None, source="watcher", folder=False):
        """Note that a file (or every file in a folder) may have changed.

        moved_from is the old path of a renamed or moved file; it lets the scanner keep the
        file's identity. A chain of renames A -> B -> C is kept as one move from A to C.
        """
        path = os.path.abspath(path)
        now = self._clock()
        with self._lock:
            items = self._folders if folder else self._files
            if moved_from:
                moved_from = os.path.abspath(moved_from)
                earlier = self._files.pop(moved_from, None)
                if earlier and earlier["moved_from"]:
                    moved_from = earlier["moved_from"]
            item = items.get(path)
            if item is None:
                if len(self._files) + len(self._folders) >= self.max_pending_files:
                    self._overflow()
                    return
                item = items[path] = {"path": path, "moved_from": None, "source": source,
                                      "first_seen": now, "last_seen": now, "events": 0}
            item["last_seen"] = now
            item["events"] += 1
            if moved_from and moved_from != path:
                item["moved_from"] = moved_from

    def _overflow(self):
        if self._full_scan_reason != "overflow-scan":
            logger.warning("More than %d changed paths are waiting; one full scan will check them instead.",
                           self.max_pending_files)
        self._files.clear()
        self._folders.clear()
        self._full_scan_reason = "overflow-scan"

    def request_full_scan(self, reason):
        """Ask the service loop for a full scan (manual request, start, resume, settings change)."""
        with self._lock:
            self._full_scan_reason = self._full_scan_reason or reason

    def take_due(self, everything=False):
        """Return (files, folders, full_scan_reason) ready for the scanner and remove them from the queue."""
        now = self._clock()
        with self._lock:
            reason, self._full_scan_reason = self._full_scan_reason, None
            everything = everything or reason is not None  # a full scan comes next: settle moves first

            def ready(item):
                return (everything or now - item["last_seen"] >= self.quiet_seconds
                        or now - item["first_seen"] >= self.max_wait_seconds)
            files = [self._files.pop(path) for path in [p for p, item in self._files.items() if ready(item)]]
            folders = [self._folders.pop(path) for path in [p for p, item in self._folders.items() if ready(item)]]
        return files, folders, reason

    def clear(self):
        with self._lock:
            self._files.clear()
            self._folders.clear()
            self._full_scan_reason = None

    def status(self):
        now = self._clock()
        with self._lock:
            items = list(self._files.values()) + list(self._folders.values())
            return {
                "waiting_files": len(self._files),
                "waiting_folders": len(self._folders),
                "oldest_waiting_seconds": round(now - min(item["first_seen"] for item in items), 1) if items else 0,
                "full_scan_requested": self._full_scan_reason,
            }
