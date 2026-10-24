"""Copy a named organization snapshot; originals are read, never changed or executed."""
import copy
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid

from Services.ConfigService import select_system_path
from Services.OrganizationService import plan_digest
from Services.ScanService import _is_link
from Services.VersionService import GitError, ignore_pattern
from Services.ResourceGovernor import ResourceCancelled, checkpoint as resource_checkpoint

logger = logging.getLogger("code-steward.execution")
PROGRAM_FOLDER = Path(__file__).resolve().parents[1]
# The library may be a folder inside the program folder (library/ by default), but never one of these.
PROGRAM_PARTS = [PROGRAM_FOLDER / name for name in (".git", "venv", ".venv", "Services", "Controllers", "ai", "docs", "__pycache__")]
HASH = re.compile(r"^[0-9a-f]{64}$")
DEVICES = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$", re.I)
PINNED = os.open in os.supports_dir_fd and os.link in os.supports_dir_fd


def now():
    return datetime.now(timezone.utc).isoformat()


def folded(value):
    return unicodedata.normalize("NFC", value).casefold()


def identity(info):
    return [info.st_dev, info.st_ino]


def inside(path, folder):
    try:
        return os.path.commonpath([folded(str(path)), folded(str(folder))]) == folded(str(folder))
    except ValueError:
        return False


class ExecutionError(ValueError):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


def target_parts(target):
    if not isinstance(target, str) or not target or "\\" in target or target.startswith("/"):
        raise ExecutionError("Target must be a relative path below the library.", 422)
    parts = target.split("/")
    for part in parts:
        if part in ("", ".", "..") or part.endswith((".", " ")) or any(ord(c) < 32 or c in '<>:"|?*' for c in part):
            raise ExecutionError("Target has an unsafe or unsupported name.", 422)
        if DEVICES.fullmatch(part.split(".")[0]) or len(part.encode("utf-8")) > 255 or folded(part) == ".git":
            raise ExecutionError("Target has a reserved or overlong name.", 422)
    return parts


class ExecutionService:
    def __init__(self, config_service, artifacts_service, organization_service, scan_service, version_service=None):
        self.config_service = config_service
        self.organization_service = organization_service
        self.scan_service = scan_service
        self.version_service = version_service  # commits every run and undo to the library's Git history
        self.database_path = artifacts_service.database_path
        self._thread = None
        self._active = None
        self._guard = threading.Lock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS execution_runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, report TEXT NOT NULL)")
        self._recover()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _save(self, report):
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO execution_runs VALUES (?, ?, ?)",
                       (report["run_id"], report["created_at"], json.dumps(report)))

    def get_run(self, run_id):
        with self._connect() as db:
            row = db.execute("SELECT report FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self):
        with self._connect() as db:
            row = db.execute("SELECT report FROM execution_runs ORDER BY created_at DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def status(self):
        active = self._active
        return {"state": "running" if active else "idle", "active_run_id": active[0] if active else None,
                "last_run": self.latest()}

    def wait(self, timeout=None):
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        return not thread or not thread.is_alive()

    def _claim(self):
        if not self.organization_service.operation_lock.acquire(blocking=False):
            raise ExecutionError("An organization operation is already in progress.")

    def _running(self):
        if not self.scan_service._is_service_running():
            raise ExecutionError("Start or resume the service before organizing files.")

    def _plan(self, plan_id):
        plan = self.organization_service.latest()
        if plan is None:
            raise ExecutionError("Build and review an organization plan first.", 404)
        if not plan_id or plan_id != plan.get("plan_id") or plan_id != plan_digest(plan):
            raise ExecutionError("The plan changed. Review the new plan before executing it.")
        return copy.deepcopy(plan)

    def _library(self):
        written = Path(select_system_path(self.config_service.get("organization", "library_path",
                                    default="library"), setting="organization.library_path"))
        if _is_link(written):
            raise ExecutionError("The library cannot be a link or junction.", 422)
        library = Path(os.path.realpath(written))
        if library.exists() and not library.is_dir():
            raise ExecutionError("The library path is occupied by a file.", 422)
        if not library.parent.is_dir():
            raise ExecutionError("Create the library's parent folder before executing.", 422)
        if (self.scan_service.is_denied(str(library)) or inside(PROGRAM_FOLDER, library)
                or any(inside(library, part) for part in PROGRAM_PARTS)):
            raise ExecutionError("The library cannot be a protected folder, the program folder or one of its parts.", 422)
        for root in self.scan_service.roots():
            real = Path(os.path.realpath(root))
            if inside(library, real) or inside(real, library):
                raise ExecutionError("The library and the scan folders must not contain each other.", 422)
        return library

    @contextmanager
    def _directory(self, library, target, create=False, report=None):
        """Pin directories on POSIX so a concurrently replaced parent cannot redirect a write."""
        parts = target_parts(target)[:-1]
        fd = None
        current = library.parent
        try:
            if PINNED:
                fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for name in [library.name, *parts]:
                # Case variants are a conflict on all supported systems, even case-sensitive disks.
                names = os.listdir(fd if fd is not None else current)
                matches = [item for item in names if folded(item) == folded(name)]
                if matches and matches != [name]:
                    raise ExecutionError("A parent folder has a different spelling or multiple case variants.", 422)
                path = current / name
                if not matches:
                    if not create:
                        yield None, path, False
                        return
                    if fd is None:
                        os.mkdir(path)
                    else:
                        os.mkdir(name, dir_fd=fd)
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else path.stat()
                    report["directories"].append({"path": str(path), "identity": identity(info)})
                    self._save(report)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else path.lstat()
                if not stat.S_ISDIR(info.st_mode) or _is_link(path):
                    raise ExecutionError("A target parent is a link, junction or file.", 422)
                if fd is not None:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                current = path
            yield fd, current, True
        finally:
            if fd is not None:
                os.close(fd)

    def _target(self, library, target, expected):
        parts = target_parts(target)
        if os.name == "nt" and len(str(library / target)) >= 260:
            raise ExecutionError("Target exceeds the supported Windows path length.", 422)
        with self._directory(library, target) as (fd, parent, exists):
            if not exists:
                return "would-copy"
            names = os.listdir(fd if fd is not None else parent)
            matches = [item for item in names if folded(item) == folded(parts[-1])]
            if not matches:
                return "would-copy"
            if matches != [parts[-1]]:
                raise ExecutionError("A target has a different spelling or multiple case variants.", 422)
            path = parent / parts[-1]
            info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False) if fd is not None else path.lstat()
            if not stat.S_ISREG(info.st_mode) or _is_link(path):
                raise ExecutionError("Target is a link, junction or directory.", 422)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
            opened = os.open(parts[-1] if fd is not None else path, flags, **({"dir_fd": fd} if fd is not None else {}))
            with os.fdopen(opened, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ExecutionError("Target is no longer a regular file.", 422)
                actual = self._hash(stream)
            return "already-done" if actual == expected else "conflict"

    def _hash(self, stream, output=None, *, governed=True):
        digest = hashlib.sha256()
        while True:
            if governed:
                resource_checkpoint(self)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        return digest.hexdigest()

    @contextmanager
    def _source(self, action, records):
        path = action.get("from_path")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ExecutionError("Source has no absolute path.", 422)
        root = self.scan_service.root_of(path)
        if root is None or not self.scan_service.in_scope(path) or _is_link(root) or _is_link(path):
            raise ExecutionError("Source is no longer in scope or is a link; build a new plan.")
        if not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
            raise ExecutionError("Source is no longer a regular file; build a new plan.")
        relative = Path(os.path.relpath(path, root)).as_posix()
        record = records.get((root, relative))
        if record is None or relative != action.get("from"):
            raise ExecutionError("Source does not match the plan's file record.", 422)
        expected = record.get("sha256", "")
        if not isinstance(expected, str) or not HASH.fullmatch(expected):
            raise ExecutionError("Source has no SHA-256. Increase the scan size limit and build a new plan.")
        fd = None
        try:
            if PINNED:
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                components = relative.split("/")
                for part in components[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                opened = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=fd)
            else:
                opened = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            with os.fdopen(opened, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ExecutionError("Source is no longer a regular file.")
                yield stream, expected
        finally:
            if fd is not None:
                os.close(fd)

    def _actions(self, plan):
        actions = copy.deepcopy(plan["actions"])
        sensitive = sorted({action["path"] for action in actions if action["action"] == "keep-out-of-git"})
        if sensitive:
            # Anchor and escape literal Git wildcards and spaces; never rewrite an existing .gitignore.
            text = "# Sensitive files excluded by Code Steward\n" + "\n".join(ignore_pattern(path) for path in sensitive) + "\n"
            actions.insert(0, {"action": "write", "to": ".gitignore", "content": text, "safety": True})
        return actions

    @staticmethod
    def _conflicts(actions):
        destinations = []
        invalid = {}
        for index, action in enumerate(actions):
            if action["action"] in ("copy", "write"):
                try:
                    parts = [folded(part) for part in target_parts(action.get("to"))]
                    destinations.append((index, parts))
                except ExecutionError as error:
                    invalid[index] = str(error)
        for n, (index, parts) in enumerate(destinations):
            for other, other_parts in destinations[n + 1:]:
                if parts[:len(other_parts)] == other_parts or other_parts[:len(parts)] == parts:
                    invalid[index] = invalid[other] = "Targets collide by case or file/folder prefix."
                else:
                    a = actions[index]["to"].split("/")
                    b = actions[other]["to"].split("/")
                    for left, right in zip(a[:-1], b[:-1]):
                        if folded(left) != folded(right):
                            break
                        if left != right:
                            invalid[index] = invalid[other] = "Parent folders differ only by case."
        return invalid

    def _inspect(self, action, records, library, invalid=None):
        action.pop("previous_blob", None)
        action.pop("previous_hash", None)
        if invalid:
            return "conflict", invalid, None
        kind = action["action"]
        if kind == "copy" and action.get("sensitive"):
            parents = Path(action["to"]).parents
            if action.get("sensitive_ignore_conflict") or any(parent != Path(".") and os.path.lexists(library / parent / ".gitignore") for parent in parents):
                return "skipped", "A nested .gitignore may override protection; sensitive file was skipped.", None
        if kind == "keep-out-of-git":
            return "skipped", "Protected by the generated .gitignore.", None
        if kind in ("copy", "skip"):
            try:
                with self._source(action, records) as (stream, expected):
                    actual = self._hash(stream)
                if actual != expected:
                    return "skipped", "Source changed; build a new plan.", None
            except (OSError, ExecutionError) as error:
                return "skipped", str(error), None
            if kind == "skip":
                # Validate both duplicate and retained source. A changed retained file invalidates the skip.
                kept = next((item for item in records.values() if item.get("root_folder") == self.scan_service.root_of(action["from_path"])
                             and item.get("path") == action.get("same_as")), None)
                if not kept or kept.get("sha256") != expected:
                    return "skipped", "Duplicate identity changed; build a new plan.", None
                retained = {"from": kept["path"], "from_path": os.path.join(kept["root_folder"], *kept["path"].split("/"))}
                try:
                    with self._source(retained, records) as (stream, same):
                        if self._hash(stream) != same:
                            return "skipped", "Retained duplicate changed; build a new plan.", None
                except (OSError, ExecutionError) as error:
                    return "skipped", str(error), None
                return "skipped", "Exact duplicate; no second copy requested.", None
        elif kind == "write":
            expected = hashlib.sha256(action["content"].encode("utf-8")).hexdigest()
        else:
            return "failed", "Unknown action.", None
        try:
            status = self._target(library, action["to"], expected)
            if status == "conflict":
                previous = self._replaceable(library, action["to"])
                if previous:
                    # The library holds an older version that Code Steward wrote and committed; Git keeps it.
                    action["previous_blob"], action["previous_hash"] = previous
                    return "would-update", None, expected
            return status, "Existing contents differ." if status == "conflict" else None, expected
        except (OSError, ExecutionError) as error:
            return "conflict", str(error), expected

    def _replaceable(self, library, target):
        """(Git blob, SHA-256) of a library file that is Code Steward's own unchanged commit, else None."""
        if self.version_service is None or not self.version_service.enabled():
            return None
        try:
            blob = self.version_service.own_unchanged(library, target)
            if not blob:
                return None
            with open(library / target, "rb") as stream:
                return blob, self._hash(stream)
        except (GitError, OSError):
            return None

    def _prepare(self, plan_id):
        self._running()
        plan, library = self._plan(plan_id), self._library()
        actions = self._actions(plan)
        records = {(record["root_folder"], record["path"]): record for record in plan["files"]}
        planned_ignores = {item["to"] for item in actions if item["action"] in ("copy", "write") and Path(item["to"]).name == ".gitignore"}
        for action in actions:
            if action["action"] == "copy":
                record = records.get((self.scan_service.root_of(action.get("from_path", "")), action.get("from")), {})
                action["sensitive"] = bool(record.get("sensitive"))
                action["sensitive_ignore_conflict"] = action["sensitive"] and any(
                    parent != Path(".") and (parent / ".gitignore").as_posix() in planned_ignores for parent in Path(action["to"]).parents)
        invalid = self._conflicts(actions)
        results = []
        for index, action in enumerate(actions):
            started = time.monotonic()
            status, reason, digest = self._inspect(action, records, library, invalid.get(index))
            results.append({**action, "index": index, "status": status, "reason": reason,
                            "expected_hash": digest, "written_hash": None, "seconds": round(time.monotonic() - started, 3)})
        safety_conflict = any(item.get("safety") and item["status"] not in ("would-copy", "would-update", "already-done")
                              for item in results)
        if safety_conflict:
            for item in results:
                if item.get("sensitive") and item["status"] == "would-copy":
                    item["status"], item["reason"] = "skipped", "Existing .gitignore differs; sensitive file was skipped."
        required = sum((records[(self.scan_service.root_of(item["from_path"]), item["from"])].get("size", 0)
                        if item["action"] == "copy" else len(item.get("content", "").encode("utf-8")))
                       for item in results if item["status"] in ("would-copy", "would-update"))
        free = shutil.disk_usage(library if library.exists() else library.parent).free
        return {"plan_id": plan_id, "library_path": str(library), "mode": "preflight", "plan": plan,
                "results": results, "summary": dict(Counter(item["status"] for item in results)),
                "required_bytes": required, "free_bytes": free, "enough_space": free >= required}

    def preflight(self, plan_id):
        self._claim()
        try:
            return self._prepare(plan_id)
        finally:
            self.organization_service.operation_lock.release()

    def _new_run(self, plan_id):
        report = self._prepare(plan_id)
        if not report["enough_space"]:
            raise ExecutionError("Not enough free space for this plan.")
        report.update(run_id=uuid.uuid4().hex, mode="execute", state="running", created_at=now(), directories=[])
        self._save(report)
        return report

    def execute(self, plan_id):
        self._claim()
        try:
            return self._execute(self._new_run(plan_id))
        finally:
            self.organization_service.operation_lock.release()

    def start(self, plan_id):
        self._claim()
        try:
            report = self._new_run(plan_id)
            return self._launch(report, self._execute)
        except BaseException:
            self.organization_service.operation_lock.release()
            raise

    def _launch(self, report, work):
        with self._guard:
            token = object()  # an undo can reuse the run id, so the marker is per operation
            self._active = (report["run_id"], token)
            self._thread = threading.Thread(target=self._background, args=(report, work, token),
                                            name="code-steward-execution", daemon=True)
            self._thread.start()
        return {"run_id": report["run_id"], "state": "running"}

    def _background(self, report, work, token=None):
        try:
            work(report)
        except ResourceCancelled as error:
            report["state"], report["last_error"] = "interrupted", str(error)
            self._save(report)
            logger.info("Organization operation interrupted safely for shutdown: %s", report["run_id"])
        except Exception as error:
            report["state"], report["last_error"] = "failed", f"{type(error).__name__}: {error}"
            self._save(report)
            logger.exception("Organization operation failed: %s", report["run_id"])
        finally:
            # Release first: once the status says idle, a new operation can start.
            self.organization_service.operation_lock.release()
            with self._guard:
                if self._active and self._active[1] is token:
                    self._active = None

    def _execute(self, report):
        library = Path(report["library_path"])
        records = {(r["root_folder"], r["path"]): r for r in report["plan"]["files"]}
        safety = True
        for result in report["results"]:
            resource_checkpoint(self)
            started = time.monotonic()
            logger.info("Organization action %s: %s -> %s", result["action"], result.get("from_path", ""), result.get("to", ""))
            if result["action"] in ("copy", "write"):
                try:
                    self._running()
                    if self._library() != library:
                        raise ExecutionError("Library settings changed; build and review a new plan.")
                    if not safety and result["action"] == "copy" and result.get("sensitive"):
                        result["status"], result["reason"] = "skipped", "The .gitignore could not be installed; sensitive file was skipped."
                        self._save(report)
                        continue
                    # Recheck source, target, scope and links immediately before every operation.
                    status, reason, expected = self._inspect(result, records, library,
                            result["reason"] if result["status"] == "conflict" and result["expected_hash"] is None else None)
                    if status in ("would-copy", "would-update"):
                        self._write(report, result, records, library, expected)  # sets done or updated
                        result["reason"] = None
                    else:
                        result["status"], result["reason"] = status, reason
                        if status == "already-done":
                            result["written_hash"] = expected
                except (OSError, ExecutionError) as error:
                    result["status"] = "skipped" if isinstance(error, ExecutionError) and str(error).startswith("Source changed") else "failed"
                    result["reason"] = str(error)
                if result.get("safety"):
                    safety = result["status"] in ("done", "updated", "already-done")
            else:
                result["status"], result["reason"], _ = self._inspect(result, records, library)
                if result["action"] == "keep-out-of-git" and not safety:
                    result["status"], result["reason"] = "failed", "Could not install .gitignore; copying was blocked."
            result["seconds"] += round(time.monotonic() - started, 3)
            self._save(report)
            logger.log(logging.WARNING if result["status"] in ("failed", "conflict") else logging.INFO,
                       "Organization result %s: %s (%s)", result["status"], result.get("to", result.get("from_path", "")), result["reason"])
        report["summary"] = dict(Counter(item["status"] for item in report["results"]))
        self._commit(report, "execute")  # before "complete": the run holds the operation until it is committed
        report["state"], report["finished_at"] = "complete", now()
        self._save(report)
        return report

    def _write(self, report, result, records, library, expected):
        name = target_parts(result["to"])[-1]
        temporary = f".code-steward-{uuid.uuid4().hex}.tmp"
        with self._directory(library, result["to"], create=True, report=report) as (fd, parent, _):
            kwargs = {"dir_fd": fd} if fd is not None else {}
            temp_path = parent / temporary
            opened = os.open(temporary if fd is not None else temp_path,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600, **kwargs)
            published = False
            recorded_done = False
            aside = None
            try:
                with os.fdopen(opened, "wb") as output:
                    if result["action"] == "copy":
                        with self._source(result, records) as (source, source_hash):
                            actual = self._hash(source, output)
                        if actual != expected or actual != source_hash:
                            raise ExecutionError("Source changed while copying; build a new plan.")
                    else:
                        output.write(result["content"].encode("utf-8"))
                    output.flush()
                    os.fsync(output.fileno())
                    result.update(staged_path=str(temp_path), identity=identity(os.fstat(output.fileno())), written_hash=expected,
                                  status="staged")
                self._save(report)  # ownership intent is durable before atomic publication
                self._running()
                if (self._library() != library or not parent.is_dir() or parent.resolve() != parent
                        or fd is not None and identity(os.fstat(fd)) != identity(parent.stat())):
                    raise ExecutionError("Target folder or library changed during copying.")
                if result.get("previous_hash"):
                    aside = self._set_aside(fd, parent, name, result, report)
                if any(folded(item) == folded(name) for item in os.listdir(fd if fd is not None else parent)):
                    raise ExecutionError("A target appeared during copying; it is retained.")
                # A full-file hardlink appears atomically and fails if a target raced into place.
                if fd is not None:
                    os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                else:
                    os.link(temp_path, parent / name)
                published = True
                matches = [item for item in os.listdir(fd if fd is not None else parent) if folded(item) == folded(name)]
                if matches != [name]:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else (parent / name).lstat()
                    if identity(info) == result["identity"]:
                        os.unlink(name, dir_fd=fd) if fd is not None else (parent / name).unlink()
                        published = False
                    raise ExecutionError("A case variant appeared during publication; it is retained.")
                if fd is not None:
                    os.fsync(fd)
                result["status"] = "updated" if aside else "done"
                self._save(report)
                recorded_done = True
            finally:
                if aside and not published:
                    self._put_back(fd, parent, aside, name, result)
                elif aside and recorded_done:
                    try:  # the previous version is in the library's Git history
                        os.unlink(aside, dir_fd=fd) if fd is not None else (parent / aside).unlink()
                        result.pop("update_aside", None)
                    except OSError:
                        logger.warning("Previous version kept beside the update: %s", parent / aside)
                # Keep the ownership witness if publishing succeeded but recording completion failed.
                if not published or recorded_done:
                    try:
                        os.unlink(temporary if fd is not None else temp_path, **kwargs)
                        result.pop("staged_path", None)
                    except OSError:
                        logger.warning("Temporary ownership witness retained: %s", temp_path)

    def _hash_at(self, fd, parent, name, *, governed=True):
        opened = os.open(name if fd is not None else parent / name,
                         os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0),
                         **({"dir_fd": fd} if fd is not None else {}))
        with os.fdopen(opened, "rb") as stream:
            return self._hash(stream, governed=governed) if stat.S_ISREG(os.fstat(stream.fileno()).st_mode) else None

    def _set_aside(self, fd, parent, name, result, report):
        """Move the previous version out of the way for an update; it must still be the committed one."""
        aside = f".code-steward-old-{uuid.uuid4().hex}.tmp"
        result["update_aside"] = str(parent / aside)
        self._save(report)  # a restart puts it back if the update is interrupted
        try:
            if fd is not None:
                os.rename(name, aside, src_dir_fd=fd, dst_dir_fd=fd)
            else:
                os.rename(parent / name, parent / aside)
        except OSError:
            result.pop("update_aside", None)  # nothing was moved
            raise
        # Finish this integrity check without waiting while the public target is
        # temporarily renamed. CPU priority stays low throughout this atomic step.
        if self._hash_at(fd, parent, aside, governed=False) != result["previous_hash"]:
            self._put_back(fd, parent, aside, name, result)
            raise ExecutionError("The library copy was edited during the update; it is retained.")
        return aside

    def _put_back(self, fd, parent, aside, name, result):
        try:
            if fd is not None:
                os.link(aside, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                os.unlink(aside, dir_fd=fd)
            else:
                os.link(parent / aside, parent / name)
                (parent / aside).unlink()
            result.pop("update_aside", None)
        except OSError:
            result["recovery_path"] = str(parent / aside)
            logger.warning("Previous library version kept at %s", parent / aside)

    def undo_target(self):
        """The run an undo applies to: the newest run that still owns copies.

        Runs that created nothing (the same plan executed again) and undone runs are passed over, so
        repeating a plan never takes away the undo of the run that copied the files.
        """
        with self._connect() as db:
            rows = db.execute("SELECT report FROM execution_runs ORDER BY created_at DESC").fetchall()
        for row in rows:
            report = json.loads(row[0])
            if report.get("state") != "undone" and any(result["status"] in ("done", "updated") and result.get("identity")
                                                       for result in report["results"]):
                return report
        return None

    def _undo_report(self, run_id):
        self._running()
        self._recover()  # adopt any owned publication whose completion recording failed
        report = self.undo_target()
        if report is None:
            raise ExecutionError("There is no organization run with copies to undo.")
        if report["run_id"] != run_id:
            raise ExecutionError(f"Only the newest run that still has copies can be undone: {report['run_id']}.")
        report["state"] = "undoing"
        self._save(report)
        return report

    def undo(self, run_id):
        self._claim()
        try:
            return self._undo(self._undo_report(run_id))
        finally:
            self.organization_service.operation_lock.release()

    def start_undo(self, run_id):
        self._claim()
        try:
            return self._launch(self._undo_report(run_id), self._undo)
        except BaseException:
            self.organization_service.operation_lock.release()
            raise

    def _undo(self, report):
        library = Path(report["library_path"])
        report["undo_results"] = report.get("undo_results", [])
        interrupted = False
        undone = {item["index"] for item in report["undo_results"] if item["status"] in ("removed", "restored")}
        for result in reversed(report["results"]):
            resource_checkpoint(self)
            if result["status"] not in ("done", "updated") or not result.get("identity") or result["index"] in undone:
                continue
            item = {"index": result["index"], "to": result["to"], "status": "retained", "reason": None}
            try:
                self._running()
                if result.get("safety") and any(os.path.lexists(library / action["path"])
                        for action in report["plan"]["actions"] if action["action"] == "keep-out-of-git"):
                    raise ExecutionError("Sensitive copies remain; their .gitignore is retained.")
                # Use the saved location even when current settings now point somewhere else.
                if _is_link(library) or library.resolve() != library or self.scan_service.is_denied(str(library)):
                    raise ExecutionError("Saved library is now linked or protected.")
                with self._directory(library, result["to"]) as (fd, parent, exists):
                    if not exists:
                        raise ExecutionError("Target folder no longer exists.")
                    if (parent.resolve() != parent or fd is not None and identity(os.fstat(fd)) != identity(parent.stat())):
                        raise ExecutionError("Target folder changed before undo; copy is retained.")
                    name = target_parts(result["to"])[-1]
                    path = parent / name
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else path.lstat()
                    if not stat.S_ISREG(info.st_mode) or identity(info) != result["identity"]:
                        raise ExecutionError("Copy was edited or replaced; it is retained.")
                    quarantine = f".code-steward-undo-{uuid.uuid4().hex}.tmp"
                    result["undo_staged_path"] = str(parent / quarantine)
                    self._save(report)  # restart can restore a file interrupted during undo
                    kwargs = {"src_dir_fd": fd, "dst_dir_fd": fd} if fd is not None else {}
                    os.rename(name if fd is not None else path, quarantine if fd is not None else parent / quarantine, **kwargs)
                    try:
                        opened = os.open(quarantine if fd is not None else parent / quarantine,
                                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0),
                                **({"dir_fd": fd} if fd is not None else {}))
                        with os.fdopen(opened, "rb") as stream:
                            info = os.fstat(stream.fileno())
                            # Do not pause while the public copy is quarantined;
                            # preserve the existing rename/check/restore sequence.
                            unchanged = stat.S_ISREG(info.st_mode) and identity(info) == result["identity"] and self._hash(stream, governed=False) == result["written_hash"]
                        if not unchanged:
                            raise ExecutionError("Copy was edited or replaced; it is retained.")
                        if result["status"] == "updated":
                            self._restore_previous(fd, parent, name, library, result)
                        os.unlink(quarantine, dir_fd=fd) if fd is not None else (parent / quarantine).unlink()
                        item["status"] = "restored" if result["status"] == "updated" else "removed"
                    finally:
                        try:
                            os.stat(quarantine, dir_fd=fd, follow_symlinks=False) if fd is not None else (parent / quarantine).lstat()
                            remains = True
                        except FileNotFoundError:
                            remains = False
                        if remains:
                            try:
                                os.link(quarantine if fd is not None else parent / quarantine,
                                        name if fd is not None else path, follow_symlinks=False, **kwargs)
                                os.unlink(quarantine, dir_fd=fd) if fd is not None else (parent / quarantine).unlink()
                            except OSError:
                                item["recovery_path"] = str(parent / quarantine)
                        if not item.get("recovery_path"):
                            result.pop("undo_staged_path", None)
            except (OSError, ExecutionError) as error:
                item["reason"] = str(error)
                if not self.scan_service._is_service_running():
                    interrupted = True
            report["undo_results"] = [old for old in report["undo_results"] if old["index"] != item["index"]]
            report["undo_results"].append(item)
            self._save(report)
            logger.info("Organization undo %s: %s (%s)", item["status"], item["to"], item["reason"])
        for folder in reversed(report["directories"] if not interrupted else []):
            path = Path(folder["path"])
            try:
                if inside(path, library) and not _is_link(path) and path.resolve() == path and identity(path.stat()) == folder["identity"]:
                    path.rmdir()  # only empty, unchanged directories owned by this run
            except OSError:
                pass
        report["undo_summary"] = dict(Counter(item["status"] for item in report["undo_results"]))
        self._commit(report, "undo")
        report["state"], report["undone_at"] = "undo-interrupted" if interrupted else "undone", now()
        self._save(report)
        return report

    def _restore_previous(self, fd, parent, name, library, result):
        """Publish the version an update replaced, read back from the library's Git history."""
        if self.version_service is None:
            raise ExecutionError("The previous version cannot be read without version history.")
        content = self.version_service.blob(library, result["previous_blob"])
        if hashlib.sha256(content).hexdigest() != result["previous_hash"]:
            raise ExecutionError("The previous version in Git does not match; the update is retained.")
        temporary = f".code-steward-{uuid.uuid4().hex}.tmp"
        kwargs = {"dir_fd": fd} if fd is not None else {}
        opened = os.open(temporary if fd is not None else parent / temporary,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600, **kwargs)
        try:
            with os.fdopen(opened, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            if fd is not None:
                os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            else:
                os.link(parent / temporary, parent / name)
        finally:
            os.unlink(temporary, **kwargs) if fd is not None else (parent / temporary).unlink(missing_ok=True)

    # Version history of the library

    def _commit(self, report, kind):
        """Commit what this run or undo changed. A Git problem is recorded; the copied files stay as they are."""
        if self.version_service is None or not self.version_service.enabled():
            return
        if kind == "execute":
            changed = [item["to"] for item in report["results"]
                       if item["action"] in ("copy", "write") and item["status"] in ("done", "updated")]
            removed, facts, body = [], *self._execute_message(report)
        else:
            removed = [item["to"] for item in report["undo_results"] if item["status"] == "removed"]
            changed = [item["to"] for item in report["undo_results"] if item["status"] == "restored"]
            facts, body = self._undo_message(report)
        if not changed and not removed:
            return
        try:
            outcome = self.version_service.commit(Path(report["library_path"]), changed, removed, facts, body,
                                                  self._sensitive_paths())
            entry = {"kind": kind, **(outcome or {"commit": None, "message": "Nothing changed in the history."})}
        except Exception as error:  # a history problem never fails a run whose files are already in place
            entry = {"kind": kind, "error": f"{type(error).__name__}: {error}" if not isinstance(error, GitError) else str(error)}
            logger.warning("Library version history not updated for %s %s: %s", kind, report["run_id"], error)
        report.setdefault("commits", []).append(entry)
        self._save(report)

    @staticmethod
    def _execute_message(report):
        """(facts, body): the first fact line is the program's subject; the model sees the other lines."""
        done = [item for item in report["results"] if item["status"] in ("done", "updated") and item["action"] in ("copy", "write")]
        copies = [item for item in done if item["action"] == "copy"]
        updated = sum(item["status"] == "updated" for item in copies)
        folders = Counter(item["to"].split("/")[0] for item in copies)
        summary = Counter(item["status"] for item in report["results"])
        changed_units = {item.get("unit") for item in done}
        plural = lambda count, word: f"{count} {word}{'s' if count != 1 else ''}"
        if updated and updated == len(copies):
            first = f"Update {plural(updated, 'file')} in {plural(len(folders), 'folder')}"
        elif updated:
            first = f"Organize {len(copies) - updated} new and {updated} updated files into {plural(len(folders), 'folder')}"
        else:
            first = f"Organize {plural(len(copies), 'file')} into {plural(len(folders), 'folder')}"
        facts = [first,
                 f"New files copied: {len(copies) - updated}; files updated to a newer version: {updated}",
                 "Topic folders: " + ", ".join(f"{name} ({count} file{'s' if count != 1 else ''})" for name, count in folders.most_common()),
                 "Group folders changed: " + ", ".join(sorted({unit["folder"] for unit in report["plan"]["units"]
                                                              if unit["id"] in changed_units and unit.get("folder")})[:25])]
        body = [f"Code Steward organization run {report['run_id']}", f"Plan {report['plan_id']}",
                f"Copied {len(copies) - updated} new files, updated {updated} and wrote {len(done) - len(copies)} generated files:",
                *(f"  {name}/: {count}" for name, count in sorted(folders.items())),
                f"Skipped {summary.get('skipped', 0)}, conflicts {summary.get('conflict', 0)}, "
                f"failed {summary.get('failed', 0)}. Sensitive and build/system files are not committed."]
        return "\n".join(facts), "\n".join(body)

    @staticmethod
    def _undo_message(report):
        """An undo gets the program's subject only (one fact line), which says exactly what happened."""
        removed = sum(item["status"] == "removed" for item in report["undo_results"])
        restored = sum(item["status"] == "restored" for item in report["undo_results"])
        kept = sum(item["status"] == "retained" for item in report["undo_results"])
        facts = f"Undo organization run {report['run_id'][:8]}"
        body = (f"Code Steward undo of organization run {report['run_id']}\n"
                f"Removed {removed} unchanged copies and restored {restored} previous versions; "
                f"kept {kept} edited, replaced or protected files.")
        return facts, body

    def _sensitive_paths(self):
        """Every library path any run marked sensitive; such files are never committed."""
        with self._connect() as db:
            reports = [json.loads(row[0]) for row in db.execute("SELECT report FROM execution_runs")]
        paths = set()
        for report in reports:
            paths |= {action["path"] for action in report["plan"]["actions"] if action["action"] == "keep-out-of-git"}
            paths |= {item["to"] for item in report["results"] if item.get("sensitive") and item.get("to")}
        return paths

    def placements(self):
        """Where earlier runs put each source file: {absolute source path: library target}.

        The newest run that is not undone wins, and only targets still in the configured library count.
        New plans keep these places, so a changed file updates its copy instead of being copied again.
        """
        library = self._configured_library()
        with self._connect() as db:
            rows = db.execute("SELECT report FROM execution_runs ORDER BY created_at DESC").fetchall()
        placed = {}
        for row in rows:
            report = json.loads(row[0])
            if report.get("state") == "undone" or Path(report["library_path"]) != library:
                continue
            for item in report["results"]:
                if (item["action"] == "copy" and item["status"] in ("done", "updated", "already-done")
                        and item.get("from_path") not in placed and (library / item["to"]).is_file()):
                    placed[item["from_path"]] = item["to"]
        return placed

    def _configured_library(self):
        return Path(os.path.realpath(select_system_path(self.config_service.get(
            "organization", "library_path", default="library"), setting="organization.library_path")))

    def repository_status(self):
        library = self._configured_library()
        if self.version_service is None or not self.version_service.enabled():
            return {"path": str(library), "repository": False, "message": "Version history is off (organization.git)."}
        try:
            return self.version_service.status(library)
        except GitError as error:
            return {"path": str(library), "repository": None, "error": str(error)}

    def repair_repository(self):
        self._claim()
        try:
            library = self._configured_library()
            if self.version_service is None or not (library / ".git").is_dir():
                raise ExecutionError("The library has no version history yet.", 404)
            try:
                return self.version_service.repair(library)
            except GitError as error:
                raise ExecutionError(f"Repair failed: {error}", 500) from error
        finally:
            self.organization_service.operation_lock.release()

    def _recover(self):
        with self._connect() as db:
            reports = [json.loads(row[0]) for row in db.execute("SELECT report FROM execution_runs")]
        for report in reports:
            active = report["state"] in ("running", "undoing")
            if not active and not any(result.get("staged_path") or result.get("undo_staged_path") or result.get("update_aside")
                                      for result in report["results"]):
                continue
            library = Path(report["library_path"])
            for result in report["results"]:
                aside_path = result.get("update_aside")
                if aside_path:
                    aside, target = Path(aside_path), library / result["to"]
                    try:
                        handled = False
                        if inside(aside, library) and aside.resolve() == aside:
                            if os.path.lexists(target) and identity(target.lstat()) == result.get("identity"):
                                aside.unlink()  # the update was published; Git keeps the previous version
                                handled = True
                            elif not os.path.lexists(target):
                                os.link(aside, target, follow_symlinks=False)
                                aside.unlink()
                                handled = True
                        if handled:
                            result.pop("update_aside", None)
                        else:
                            result["update_recovery_path"] = aside_path  # another file took the name meanwhile
                    except OSError:
                        result["update_recovery_path"] = aside_path
                undo_path = result.get("undo_staged_path")
                if undo_path:
                    temporary = Path(undo_path)
                    target = library / result["to"]
                    try:
                        if (temporary.resolve() == temporary and target.resolve() == target
                                and inside(temporary, library) and temporary.is_file()):
                            os.link(temporary, target, follow_symlinks=False)
                            temporary.unlink()
                            result.pop("undo_staged_path", None)
                    except OSError:
                        result["undo_recovery_path"] = undo_path
                if result["status"] == "done" and result.get("staged_path"):
                    temporary = Path(result["staged_path"])
                    try:
                        if (temporary.resolve() == temporary and inside(temporary, library)
                                and identity(temporary.lstat()) == result["identity"]):
                            temporary.unlink()
                            result.pop("staged_path", None)
                    except OSError:
                        pass
                if result["status"] in ("staged", "failed") and result.get("staged_path"):
                    try:
                        target = library / result["to"]
                        temporary = Path(result["staged_path"])
                        safe = target.resolve() == target and temporary.resolve() == temporary and inside(temporary, library)
                        if safe and identity(temporary.lstat()) == result["identity"]:
                            if target.exists() and identity(target.lstat()) == result["identity"]:
                                result["status"] = "done"
                            else:
                                result["status"] = "failed"
                            temporary.unlink()
                    except OSError:
                        result["status"] = "failed"
                    result["reason"] = "Interrupted during atomic publication; ownership was checked on restart."
                    if result["status"] == "done" and result.get("previous_hash"):
                        result["status"] = "updated"
                elif result["status"] in ("would-copy", "would-update"):
                    result["status"], result["reason"] = "skipped", "Run interrupted; execute a reviewed plan again."
            if active:
                report["state"] = "interrupted"
            report["summary"] = dict(Counter(item["status"] for item in report["results"]))
            self._save(report)
