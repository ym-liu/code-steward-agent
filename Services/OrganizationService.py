import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ai.common import AIError
from ai.explain.facts import static_facts
from ai.organize import code_role, load_topics, mark_duplicates, page_html, plan_for, short_dependency
from Services.AnalysisService import waiting_reason
from Services.ResourceGovernor import ResourceCancelled, checkpoint as resource_checkpoint

logger = logging.getLogger("code-steward.organization")
PROGRAM_FOLDER = Path(__file__).resolve().parents[1]
CODE_TYPES = {"script", "wrapper"}
SOURCE_LIMIT = 200000  # characters read from a code file to find the files it uses


def plan_digest(plan):
    data = {key: value for key, value in plan.items() if key != "plan_id"}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class OrganizationService:
    """The organization plan: one topic and one proposed folder for every group of files (a dry run).

    The plan comes from the organizer in ai/organize.py (plan_for) fed with the product's data:
    file types from ClassificationService, AI explanations from AnalysisService and program facts of
    each code file. Files that use each other (imports, started scripts, paths in code) and folders
    with a README or LICENSE stay together. The model picks one topic per group from the editable
    topic list; keyword hints check it and replace it when Ollama is unavailable. Nothing is copied,
    moved or deleted. Topic answers are saved, so a new plan only asks about groups that changed.
    Every file is read again for a plan, so the plan describes the files as they are now.
    """

    def __init__(self, config_service, artifacts_service, analysis_service, scan_service):
        self.config_service = config_service
        self.artifacts_service = artifacts_service
        self.analysis_service = analysis_service
        self.scan_service = scan_service
        self.database_path = artifacts_service.database_path
        self.state = "no plan yet"
        self.progress = None
        self.last_error = None
        self._lock = threading.Lock()
        self.operation_lock = threading.Lock()  # shared by plan building, execution and undo
        self.placements = None  # callable set by ServiceContainer: where earlier runs copied files
        self._building = False
        self._thread = None
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS organization_plans (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    created_at TEXT NOT NULL,
                    plan TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS topic_answers (
                    key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    answer TEXT NOT NULL
                )
            """)
            if connection.execute("SELECT 1 FROM organization_plans").fetchone():
                self.state = "ready"

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def topics(self):
        path = Path(os.path.expanduser(self.config_service.get("organization", "topics_file",
                                                               default="ai/settings/topics.json")))
        return load_topics(path if path.is_absolute() else PROGRAM_FOLDER / path)

    # Saved topic answers (plan_for's topic_cache, for one model version: see build)

    def get(self, key):
        with self._connect() as connection:
            row = connection.execute("SELECT answer FROM topic_answers WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, answer):
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO topic_answers (key, created_at, answer) VALUES (?, ?, ?)",
                               (key, datetime.now(timezone.utc).isoformat(), json.dumps(answer)))

    # Building a plan

    def start(self, use_ai=True):
        """Build a new plan in the background. Returns False when one is already being built."""
        with self._lock:
            if self._building or not self.operation_lock.acquire(blocking=False):
                return False
            self._building = True
            self.state, self.progress, self.last_error = "building", None, None
            self._thread = threading.Thread(target=self._build_in_background, args=(use_ai,),
                                            name="code-steward-organization", daemon=True)
            self._thread.start()
        return True

    def wait(self, timeout=None):
        """Wait for a plan being built (at shutdown). Returns False when it is still being built."""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _build_in_background(self, use_ai):
        try:
            self.build(use_ai, _claimed=True)
        except ResourceCancelled as error:
            self.state, self.last_error = "interrupted", str(error)
            logger.info("Organization planning interrupted safely for shutdown.")
        except Exception as error:
            self.state, self.last_error = "failed", f"{type(error).__name__}: {error}"
            logger.exception("Building the organization plan failed.")
        finally:
            with self._lock:
                self._building = False
            self.operation_lock.release()

    def build(self, use_ai=True, _claimed=False):
        if not _claimed and not self.operation_lock.acquire(blocking=False):
            raise ValueError("An organization operation is already in progress.")
        try:
            return self._build(use_ai)
        finally:
            if not _claimed:
                self.operation_lock.release()

    def _build(self, use_ai=True):
        """Build, save and return a plan for every root folder."""
        resource_checkpoint(self)
        started = time.monotonic()
        topics = self.topics()
        client, tag, model_digest, topics_from = self._client(use_ai)
        # Saved answers belong to one model version: a newer download of the same model asks again.
        topic_cache = SimpleNamespace(get=lambda key: self.get(f"{model_digest}:{key}"),
                                      put=lambda key, answer: self.put(f"{model_digest}:{key}", answer))
        plan = {"files": [], "units": [], "loose": [], "actions": [], "unavailable_roots": []}
        taken = set()  # proposed names stay unique across all roots
        # Where earlier runs copied each file (absolute source path -> library target); groups keep those folders.
        placed = self.placements() if self.placements else {}
        for number, root in enumerate(self.scan_service.roots(), 1):
            resource_checkpoint(self)
            if self.scan_service.is_denied(root) or self.scan_service.in_library(root):
                continue
            if not os.path.isdir(root):
                plan["unavailable_roots"].append(root)  # for example an unplugged drive; planned when it is back
                continue
            records, sources, facts = self._records(root)
            if not records:
                continue
            here = {Path(os.path.relpath(path, root)).as_posix(): target for path, target in placed.items()
                    if os.path.dirname(path) == root or os.path.dirname(path).startswith(root.rstrip(os.sep) + os.sep)}
            part = plan_for(records, sources, facts, client, tag, topics, progress=self._report,
                            topic_cache=topic_cache, taken=taken, placed=here,
                            checkpoint=lambda: resource_checkpoint(self))
            self._merge(plan, part, root, f"r{number}-")
        work = [unit for unit in plan["units"] if not unit["duplicate_of"]]
        without_answer = sum(unit.get("ai_topic") is None for unit in work)  # failed, unreadable or not asked
        if client is not None and without_answer:
            topics_from += f"; keyword hints for {without_answer} group(s) without an AI answer"
        plan.update(
            version=1, mode="dry-run", created_at=datetime.now(timezone.utc).isoformat(),
            seconds=round(time.monotonic() - started, 1), model=tag, model_digest=model_digest,
            topics_from=topics_from, topics_version=topics["version"],
            summary={"files": len(plan["files"]),
                     "types": dict(Counter(record["type"] for record in plan["files"])),
                     "sensitive": sum(record["sensitive"] for record in plan["files"]),
                     "duplicates": sum(bool(record["duplicate_of"]) for record in plan["files"]),
                     "code_files_not_explained_yet": sum(record["type"] in CODE_TYPES and not record["explained"]
                                                         for record in plan["files"]),
                     "groups": len(work), "folders": len({unit["folder"] for unit in work if unit.get("folder")}),
                     "needs_review": sum(action["action"] == "copy" and action["to"].startswith("needs-review/")
                                         for action in plan["actions"])})
        with self._connect() as connection:
            plan["plan_id"] = plan_digest(plan)
            connection.execute("INSERT OR REPLACE INTO organization_plans (id, created_at, plan) VALUES (1, ?, ?)",
                               (plan["created_at"], json.dumps(plan)))
        self.state, self.progress = "ready", None
        logger.info("Organization plan ready: %d groups, %d proposed folders (%s, %.1f s).",
                    plan["summary"]["groups"], plan["summary"]["folders"], topics_from, plan["seconds"])
        return plan

    def _client(self, use_ai):
        """(client, model, model version, how topics are chosen); no client when only keyword hints are used.

        The model gets the same checks as for AI explanations: installed, local (not cloud-backed)
        and no other model loaded.
        """
        if not use_ai:
            return None, None, None, "keyword hints only (AI not requested)"
        try:
            client, tag, model_digest = self.analysis_service.topic_model()
        except AIError as error:
            if error.code == "off":
                return None, None, None, "keyword hints only (AI explanations are off)"
            reason = waiting_reason(error, self.analysis_service.tag)
            logger.warning("Organization plan without AI topics: %s", reason)
            return None, None, None, f"keyword hints only ({reason})"
        return client, tag, model_digest, "AI, checked by keyword hints"

    def _records(self, root):
        """One record per saved file under root, in the form the organizer (ai/organize.py) expects.

        Every file is read again first. Files that changed since the last scan are scanned again, so
        hashes, types and explanations describe the files as they are now. A file that still differs
        (it keeps changing, or cannot be read) keeps the hash of what was read and gets no explanation.
        """
        artifacts, current = self._read_files(root)
        changed = [path for path, (file_hash, _) in current.items() if file_hash != artifacts[path].get("hash")]
        if changed:
            logger.info("%d files changed since the last scan; checking them before planning.", len(changed))
            self.scan_service.process(files=[{"path": path, "source": "plan-check"} for path in changed])
            artifacts, current = self._read_files(root)
        records, sources, facts = [], {}, {}
        for path, artifact in artifacts.items():
            resource_checkpoint(self)
            file_hash, source = current[path]
            rel = Path(os.path.relpath(path, root)).as_posix()
            same = file_hash == artifact.get("hash")
            result = self.analysis_service.analysis_for(artifact) if same else {}
            analysis = result.get("analysis") if result.get("status") == "done" else None
            kind = artifact.get("artifact_type") or "unknown"
            record = {"path": rel, "size": artifact.get("size_bytes") or 0,
                      "sha256": file_hash or f"not hashed: {rel}", "type": kind, "role": None,
                      "language": artifact.get("language"), "sensitive": bool(artifact.get("sensitive")),
                      "sensitive_reason": artifact.get("sensitive_reason"), "duplicate_of": None,
                      "confidence": artifact.get("confidence") or 0.3,
                      "reasons": list(artifact.get("classification_reasons") or []),
                      "summary": (analysis or {}).get("purpose"),
                      "dependencies": [short_dependency(item) for item in (analysis or {}).get("dependencies") or []][:8],
                      "starts": [], "explained": analysis is not None,
                      "check_notes": len(result.get("check_notes") or [])}
            if not same:
                record["reasons"].append("Changed or unreadable while the plan was built; check it again.")
            if analysis is not None:
                record["usage"], record["unknowns"] = analysis.get("usage") or [], analysis.get("unknowns") or []
            if kind in CODE_TYPES and source is not None:
                suffix = Path(rel).suffix.lower()
                sources[rel], facts[rel] = source, static_facts(source, suffix)
                if kind == "script":
                    record["role"] = code_role(rel, facts[rel], analysis, source, suffix)[0]
            records.append(record)
        mark_duplicates(records, checkpoint=lambda: resource_checkpoint(self))
        return records, sources, facts

    def _read_files(self, root):
        """The saved files under root, and (hash, code text) of each as it is now."""
        artifacts = {artifact["path"]: artifact for artifact in self.artifacts_service.list_artifacts()["items"]
                     if self.scan_service.root_of(artifact["path"]) == root}
        return artifacts, {path: self._read(artifact) for path, artifact in artifacts.items()}

    def _read(self, artifact):
        """(hash, text) of a saved file as it is now; text only for code files.

        A file larger than max_file_size_bytes has no hash (it never had one); a file that is gone,
        unreadable or replaced by a link gives (None, None).
        """
        code, hashed = artifact.get("artifact_type") in CODE_TYPES, bool(artifact.get("hash"))
        if os.path.islink(artifact["path"]) or not (code or hashed):
            return None, None
        try:
            with open(artifact["path"], "rb") as stream:
                hasher = hashlib.sha256() if hashed else None
                head, remaining = [], SOURCE_LIMIT * 4
                while hashed or remaining:
                    resource_checkpoint(self)
                    chunk = stream.read(65536 if hashed else min(65536, remaining))
                    if not chunk:
                        break
                    if hasher is not None:
                        hasher.update(chunk)
                    if code and remaining:
                        part = chunk[:remaining]
                        head.append(part)
                        remaining -= len(part)
                raw = b"".join(head)
        except OSError:
            return None, None
        text = None
        if code:
            text = raw[:SOURCE_LIMIT * 4].decode("utf-8", errors="replace")
            text = None if "\x00" in text else text[:SOURCE_LIMIT]
        return (hasher.hexdigest() if hasher is not None else None), text

    @staticmethod
    def _merge(plan, part, root, prefix):
        """Add one root's plan; unit ids get a root prefix and every file keeps its full path."""
        for unit in part["units"]:
            unit["id"] = prefix + unit["id"]
            unit["root_folder"] = root
        for action in part["actions"]:
            if action.get("unit"):
                action["unit"] = prefix + action["unit"]
            if "from" in action:
                action["from_path"] = os.path.join(root, *action["from"].split("/"))
        for record in part["files"]:
            record["root_folder"] = root
        plan["units"] += part["units"]
        plan["actions"] += part["actions"]
        plan["files"] += part["files"]
        plan["loose"] += [os.path.join(root, *path.split("/")) for path in part["loose"]]

    def _report(self, text):
        self.progress = text.strip()
        logger.debug("Organization plan: %s", self.progress)

    # Reading the plan

    def latest(self):
        with self._connect() as connection:
            row = connection.execute("SELECT plan FROM organization_plans WHERE id = 1").fetchone()
        plan = json.loads(row[0]) if row else None
        if plan is not None:
            plan.setdefault("plan_id", plan_digest(plan))
        return plan

    def page(self):
        plan = self.latest()
        return page_html(plan, self.topics()) if plan else None

    def status(self):
        plan = self.latest()
        return {"state": self.state, "progress": self.progress, "last_error": self.last_error,
                "plan_id": plan["plan_id"] if plan else None,  # send it to /organization/preflight and /execute
                "last_plan_at": plan["created_at"] if plan else None,
                "summary": plan["summary"] if plan else None,
                "topics_from": plan["topics_from"] if plan else None}
