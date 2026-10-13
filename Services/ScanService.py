import os
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone

from Services.ClassificationService import HEAD_BYTES, ClassificationService

logger = logging.getLogger("code-steward.scanner")
CHECKPOINT_FILE = "scan_checkpoint.json"

# Operating-system folders are never scanned, even inside a configured root.
# Add more folders with scanning.deny_paths in config.yaml.
if os.name == "nt":
    SYSTEM_FOLDERS = [os.environ.get(name, default) for name, default in (
        ("SystemRoot", r"C:\Windows"), ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"), ("ProgramData", r"C:\ProgramData"))]
else:
    SYSTEM_FOLDERS = ["/System", "/Library", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/dev", "/proc",
                      "/sys", "/boot"]

# Temporary files that editors, browsers and operating systems create; they are never registered.
IGNORED_NAMES = {".ds_store", "thumbs.db", "desktop.ini", "4913"}
IGNORED_SUFFIXES = (".swp", ".swo", ".swx", ".tmp", ".temp", ".part", ".crdownload", ".download", "~")
IGNORED_PREFIXES = (".#", "~$")


def _inside(path, folder):
    path, folder = os.path.normcase(path), os.path.normcase(folder).rstrip(os.sep)
    return path == folder or path.startswith(folder + os.sep)


def _is_link(path):
    """Symbolic links, and Windows junctions, which os.path.islink does not report."""
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path)


class ScanService:
    """Keeps the saved artifacts in step with the files under the configured roots.

    process() checks single paths (from the event aggregator) and, when asked, walks every root.
    Single paths are always compared by content; a full walk skips files whose size and
    modification time are unchanged, so it stays cheap. Only one scan runs at a time.
    """

    def __init__(self, config_service, logs_service, artifacts_service=None, code_steward_service=None,
                 classifier=None):
        self.config_service = config_service
        self.logs_service = logs_service
        self.artifacts_service = artifacts_service
        self.code_steward_service = code_steward_service
        self.classifier = classifier or ClassificationService()
        self.own_paths = set()   # the agent's own files (log, config); set by ServiceContainer
        self.own_folder = None   # the program folder; skipped when a wider root contains it
        # Told about every saved change: listener.artifact_changed(artifact) and listener.artifact_removed(path).
        # AnalysisService uses this to queue AI explanations.
        self.listeners = []
        self._lock = threading.Lock()
        self._is_scanning   = False
        self._last_scan_at  = None
        self._last_full_scan_at = None
        self._last_results  = []
        self._last_summary = None
        self._checkpoint = {}
        self._denied_folders = ((), [])  # (deny settings, the same folders with links resolved)

    def get_status(self):
        return {
            "is_scanning":   self._is_scanning,
            "last_scan_at":  self._last_scan_at.isoformat() if self._last_scan_at else None,
            "files_found":   len(self._last_results),
            "last_full_scan_at": self._last_full_scan_at.isoformat() if self._last_full_scan_at else None,
            "last_summary": self._last_summary,
            "roots": [{"path": root, "available": os.path.isdir(root)} for root in self.roots()],
        }

    def _is_service_running(self) -> bool:
        if self.code_steward_service is None:
            return True
        return self.code_steward_service.state.value == "running"

    # Settings and scope

    def setting(self, key, default):
        if self.config_service is None:
            return default
        value = self.config_service.get("scanning", key, default=default)
        return default if value is None else value

    def roots(self):
        roots = self.setting("root_paths", [])
        if isinstance(roots, str):
            roots = [roots]
        return [os.path.abspath(os.path.expanduser(root)) for root in roots if isinstance(root, str) and root.strip()]

    def root_of(self, path):
        return next((root for root in self.roots() if _inside(path, root)), None)

    def canonical(self, path):
        """Spell a reported path the way scans store it (macOS reports /private/var for /var)."""
        path = os.path.abspath(path)
        if self.root_of(path):
            return path
        for root in self.roots():
            real = os.path.realpath(root)
            if real != root and _inside(path, real):
                return root + path[len(real.rstrip(os.sep)):]
        return path

    def is_denied(self, path):
        """True inside a system folder or a deny_paths folder, also when a link leads there."""
        folders = tuple(os.path.abspath(os.path.expanduser(folder))
                        for folder in SYSTEM_FOLDERS + list(self.setting("deny_paths", [])))
        if self._denied_folders[0] != folders:
            # Resolved once per setting: /etc is /private/etc on macOS, and a deny folder can be a link.
            self._denied_folders = (folders, sorted(set(folders) | {os.path.realpath(folder) for folder in folders}))
        path = os.path.abspath(path)
        return any(_inside(spelling, folder) for spelling in {path, os.path.realpath(path)}
                   for folder in self._denied_folders[1])

    @staticmethod
    def _behind_link(folder, root):
        """True when folder, or a folder between it and root, is a link: links are never followed."""
        while _inside(folder, root) and os.path.normcase(folder) != os.path.normcase(root):
            if _is_link(folder):
                return True
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
        return False

    def _root_unavailable(self, path):
        """True when the root folder of path is missing (for example an unplugged drive): its records are kept."""
        root = self.root_of(path)
        return root is not None and not os.path.isdir(root)

    def _own_folder_inside(self, path, root):
        """True for the agent's own folder (code, database, logs) when a wider root contains it."""
        return bool(self.own_folder) and not _inside(root, self.own_folder) and _inside(path, self.own_folder)

    def _extension_allowed(self, name):
        extensions = self.setting("include_extensions", [])
        return not extensions or any(name.lower().endswith(extension.lower()) for extension in extensions)

    def in_scope(self, path, folder=False):
        """True when a path may be registered: inside a root, not excluded, protected or temporary,
        and not reached through a linked folder (a link could lead outside the root or into a denied folder)."""
        path = os.path.abspath(path)
        root = self.root_of(path)
        if root is None or self.is_denied(path) or self._own_folder_inside(path, root):
            return False
        if self._behind_link(path if folder else os.path.dirname(path), root):
            return False
        relative = os.path.relpath(path, root)
        parts = [] if relative == "." else relative.split(os.sep)
        excluded = set(self.setting("exclude_folders", []))
        if any(part in excluded for part in (parts if folder else parts[:-1])):
            return False
        if folder:
            return True
        name = os.path.basename(path)
        lower = name.lower()
        if lower in IGNORED_NAMES or lower.endswith(IGNORED_SUFFIXES) or name.startswith(IGNORED_PREFIXES):
            return False
        if os.path.normcase(path) in self.own_paths:
            return False
        if self.artifacts_service is not None and self.artifacts_service.is_storage_file(path):
            return False
        return self._extension_allowed(name)

    # Scanning

    def enqueue_manual_scan(self, events=None):
        """Scan now and return the result (tests and direct calls). The API queues scans instead."""
        if not self._is_service_running():
            logger.warning("Scan blocked: service is %s; start or resume it before scanning.",
                           self.code_steward_service.state.value)
            return {"message": "Service is not running. Start the service before scanning."}
        if self._is_scanning:
            logger.warning("Scan blocked: another scan is already in progress.")
            return {"message": "Scan already in progress."}
        logger.info("Scan requested (%s, %d file events).", "file events" if events else "manual", len(events or []))
        items = [item for item in (self._item_from_event(event) for event in events or []) if item]
        run = self.process(files=items, full_scan=None if events else "manual-scan")
        return {
            "message": "Scan complete.",
            "files_found": len(run["results"]),
            "results": run["results"],
            "removed": run["removed"],
            "summary": run["summary"],
        }

    @staticmethod
    def _item_from_event(event):
        """Turn an event dictionary {event_type, source_path, destination_path} into a path to check."""
        source = event.get("source_path")
        if event.get("event_type") == "moved" and event.get("destination_path"):
            return {"path": event["destination_path"], "moved_from": source, "source": "manual-scan"}
        return {"path": source, "moved_from": None, "source": "manual-scan"} if source else None

    def process(self, files=(), folders=(), full_scan=None):
        """Check waiting paths and, when full_scan names a reason, every file under the roots.

        Moves go first so a renamed file keeps its identity. Storage errors stop the scan and are
        raised; problems with single files are logged and the scan continues.
        """
        with self._lock:
            self._is_scanning = True
            self._last_scan_at = datetime.now(timezone.utc)
            started = time.monotonic()
            run = {"results": [], "removed": [], "extension_skips": 0,
                   "counts": dict.fromkeys(("created", "modified", "moved", "deleted", "unchanged", "skipped", "errors"), 0)}
            try:
                if self.artifacts_service is None:
                    self._checkpoint = self._load_checkpoint()
                for item in sorted(files, key=lambda item: item.get("moved_from") is None):
                    self._check_file(item["path"], item.get("moved_from"), item.get("source", "watcher"), run)
                for item in folders:
                    self._check_folder(item["path"], item.get("source", "watcher"), run)
                if full_scan:
                    self._full_scan(full_scan, run)
                    self._last_full_scan_at = datetime.now(timezone.utc)
                if self.artifacts_service is None:
                    self._save_checkpoint(self._checkpoint)
            except Exception:
                logger.exception("Scan failed unexpectedly.")
                raise
            finally:
                self._is_scanning = False
            run["summary"] = {**run["counts"], "source": full_scan or "file events",
                              "seconds": round(time.monotonic() - started, 3)}
            self._last_results = run["results"]
            self._last_summary = run["summary"]
            if run["extension_skips"]:
                logger.info("Skipped %d files because their extensions are not in include_extensions.",
                            run["extension_skips"])
            counts = run["counts"]
            level = logging.INFO if full_scan or run["results"] or run["removed"] else logging.DEBUG
            logger.log(level, "Scan complete. %d new/changed files found (%d removed, %d unchanged; %s, %.1f s).",
                       len(run["results"]), counts["deleted"], counts["unchanged"], run["summary"]["source"],
                       run["summary"]["seconds"])
            return run

    def _check_file(self, path, moved_from, source, run, walked=False):
        """Compare one path with its saved record and save what changed.

        walked: found by a full scan's walk, which already checked in_scope. Such a file whose size and
        modification time are unchanged is not read. Event checks always compare contents, because
        some file systems keep the same time.
        """
        path = self.canonical(path)
        known = self._known(path)
        if not walked and not self.in_scope(path):
            if known is not None:
                self._remove(path, run, "no longer included by the settings")
            else:
                run["counts"]["skipped"] += 1
            return
        if os.path.islink(path) or not os.path.isfile(path):
            if known is not None and not self._root_unavailable(path):
                self._remove(path, run, "replaced by a link" if os.path.islink(path) else "deleted")
            return
        try:
            size = os.path.getsize(path)
        except OSError as error:
            logger.warning("Could not read file size %s: %s", path, error)
            run["counts"]["errors"] += 1
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError as error:
            logger.warning("Could not read modification time %s: %s", path, error)
            run["counts"]["errors"] += 1
            return
        version = self.classifier.VERSION
        if (walked and known is not None and known.get("size") == size and known.get("mtime") == mtime
                and known.get("classifier_version") == version):
            run["counts"]["unchanged"] += 1
            return
        too_large = size > self.setting("max_file_size_bytes", 10485760)
        file_hash = None
        if not too_large:
            file_hash = self._hash_file(path)
            if file_hash is None:
                run["counts"]["errors"] += 1
                return
        classification = self.classifier.describe(path, self._read_head(path), self.root_of(path))
        if too_large:
            classification["reasons"] = classification["reasons"] + [
                "Larger than max_file_size_bytes, so its contents were not hashed."]
        modified = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
        # Identity rule: a renamed file keeps its record at the new path. When the new path already has a
        # record (an editor saving through a renamed copy, or a file renamed over another), that path keeps
        # its record and the renamed file's old record goes.
        previous_path = self._renamed_from(path, moved_from)
        if previous_path is not None and known is not None:
            self._remove(previous_path, run, f"renamed onto {os.path.basename(path)}")
            previous_path = None
        if known is not None and (known.get("hash") == file_hash if not too_large
                                  else known.get("size") == size and known.get("mtime") == mtime):
            if known.get("mtime") != mtime or known.get("classifier_version") != version:
                self._touch(path, mtime, modified, classification)  # same contents: refresh quietly
            run["counts"]["unchanged"] += 1
            return
        change_type = "moved" if previous_path else "created" if known is None else "modified"
        record = {"path": path, "size": size, "hash": file_hash, "modified": modified, "mtime": mtime,
                  "discovered_by": source, "classification": classification}
        stored = self._store(record, change_type, previous_path)
        run["results"].append(stored)
        run["counts"][change_type] += 1
        logger.info("Artifact %s: %s%s", change_type, path,
                    " (previous path: " + previous_path + ")" if previous_path else "")

    def _renamed_from(self, path, moved_from):
        """The earlier path of a known file renamed to path, or None.

        Editors that save through a temporary file are not renames: temporary names are never registered.
        """
        if not moved_from:
            return None
        moved_from = self.canonical(moved_from)
        if (os.path.normcase(moved_from) != os.path.normcase(path) and not os.path.lexists(moved_from)
                and self._known(moved_from) is not None):
            return moved_from
        return None

    def _check_folder(self, folder, source, run):
        """A folder was created, deleted or moved: drop records for files that are gone, register new ones.

        A missing root folder (an unplugged drive) is not a deletion: its records are kept.
        """
        folder = self.canonical(folder)
        for known_path in self._known_paths():
            if _inside(known_path, folder) and os.path.normcase(known_path) != os.path.normcase(folder) \
                    and not os.path.isfile(known_path) and not self._root_unavailable(known_path):
                self._remove(known_path, run, "folder deleted or moved away")
        if os.path.isdir(folder) and self.in_scope(folder, folder=True):
            self._walk(folder, source, run, set(), set())

    def _full_scan(self, source, run):
        """Walk every root; then remove records of files that are gone or no longer included."""
        seen, failed, walked, denied = set(), set(), [], []
        for root in self.roots():
            if not os.path.isdir(root):
                logger.warning("Root path does not exist, skipping: %s", root)
                continue
            if self.is_denied(root):
                logger.warning("Root path is a system folder or in deny_paths, skipping: %s", root)
                denied.append(root)
                continue
            logger.info("Scanning: %s", root)
            self._walk(root, source, run, seen, failed)
            walked.append(root)
        for known_path in self._known_paths():
            if os.path.normcase(known_path) in seen:
                continue
            root = self.root_of(known_path)
            if root is None:
                self._remove(known_path, run, "outside the configured folders")
            elif root in denied:
                self._remove(known_path, run, "no longer included by the settings")
            elif root in walked and not any(_inside(known_path, folder) for folder in failed):
                # Records under a missing root (for example an unplugged drive) are kept.
                if not os.path.lexists(known_path):
                    self._remove(known_path, run, "deleted")
                elif not self.in_scope(known_path):
                    self._remove(known_path, run, "no longer included by the settings")

    def _walk(self, top, source, run, seen, failed):
        excluded = set(self.setting("exclude_folders", []))
        root = self.root_of(top) or top
        skipped_level = logging.INFO if source == "manual-scan" else logging.DEBUG

        def unreadable(error):
            logger.warning("Cannot open folder %s: %s", error.filename, error.strerror or error)
            failed.add(error.filename)

        for dirpath, dirnames, filenames in os.walk(top, onerror=unreadable):
            kept = []
            for name in dirnames:
                folder = os.path.join(dirpath, name)
                if name in excluded:
                    logger.log(skipped_level, "Directory skipped by exclude_folders: %s", folder)
                elif _is_link(folder) or self.is_denied(folder) or self._own_folder_inside(folder, root):
                    logger.debug("Directory skipped: %s", folder)
                else:
                    kept.append(name)
            dirnames[:] = kept
            for filename in filenames:
                path = os.path.abspath(os.path.join(dirpath, filename))
                if not self.in_scope(path):
                    if not self._extension_allowed(filename):
                        run["extension_skips"] += 1
                    continue
                seen.add(os.path.normcase(path))
                self._check_file(path, None, source, run, walked=True)

    # Storage: the artifacts database, or the JSON checkpoint when used on its own

    def _known(self, path):
        if self.artifacts_service is not None:
            return self.artifacts_service.known_state(path)
        file_hash = self._checkpoint.get(path)
        return None if file_hash is None else {"hash": file_hash}

    def is_known(self, path):
        return self._known(path) is not None

    def _known_paths(self):
        if self.artifacts_service is not None:
            return self.artifacts_service.known_paths()
        return list(self._checkpoint)

    def _store(self, record, change_type, previous_path):
        if self.artifacts_service is not None:
            stored = self.artifacts_service.classify(record, change_type=change_type, previous_path=previous_path)
            self._tell_listeners("artifact_changed", stored)
            return stored
        self._checkpoint.pop(previous_path, None)
        self._checkpoint[record["path"]] = record["hash"]
        return {**record, "change_type": change_type, "previous_path": previous_path}

    def _tell_listeners(self, method, value):
        for listener in self.listeners:
            try:
                getattr(listener, method)(value)
            except Exception:
                logger.exception("A change listener failed; the scan continues.")

    def _touch(self, path, mtime, modified, classification):
        if self.artifacts_service is not None:
            self.artifacts_service.touch(path, mtime, modified, classification)

    def _remove(self, path, run, reason):
        if self.artifacts_service is not None:
            self.artifacts_service.remove(path)
            self._tell_listeners("artifact_removed", path)
        else:
            self._checkpoint.pop(path, None)
        run["removed"].append(path)
        run["counts"]["deleted"] += 1
        logger.info("Artifact removed (%s): %s", reason, path)

    #Hashing

    def _hash_file(self, filepath) -> str | None:
        """Compute SHA-256 hash of a file. Returns None on permission error."""
        hasher = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                for chunk in iter (lambda: f.read(65536), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except PermissionError:
            logger.warning("Permission denied reading file: %s", filepath)
            return None
        except OSError as e:
            logger.warning("Could not read file %s: %s", filepath, e)
            return None

    @staticmethod
    def _read_head(path):
        try:
            with open(path, "rb") as stream:
                return stream.read(HEAD_BYTES)
        except OSError:
            return b""

    #checkpoint for scans without the artifacts database

    def _load_checkpoint(self) -> dict:
        if not os.path.exists(CHECKPOINT_FILE):
            return {}
        try:
            with open(CHECKPOINT_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Could not load checkpoint: %s", e)
            return {}

    def _save_checkpoint(self, checkpoint: dict):
        """Save the current scan checkpoint to disk"""
        try:
            with open(CHECKPOINT_FILE, "w") as f:
                json.dump(checkpoint, f, indent=2)
        except Exception as e:
            logger.warning("Could not save checkpoint: %s", e)
