import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ai_lab.common import LabError, digest
from ai_lab.explain.answer import SCHEMA_VERSION
from ai_lab.explain.checks import CHECKS_VERSION
from ai_lab.explain.config import load_config, resolve_model
from ai_lab.explain.facts import static_facts
from ai_lab.explain.files import snapshot
from ai_lab.explain.ollama_client import OllamaClient
from ai_lab.explain.request import INPUT_VERSION, PROMPT
from ai_lab.testing.runner import result_for
from Services.ClassificationService import PLACEHOLDER, SECRET

logger = logging.getLogger("code-steward.analysis")
PROGRAM_FOLDER = Path(__file__).resolve().parents[1]
# Problems on the Ollama side: the file waits and is tried again later; it is not the file's fault.
WAIT_FOR_OLLAMA = {"connection_error", "timeout", "response_error", "busy", "missing_model", "cloud_model"}
WAITING_REASONS = {
    "connection_error": "Ollama is not running; open Ollama",
    "timeout": "Ollama did not answer in time",
    "response_error": "Ollama sent an unreadable answer",
    "busy": "another model is loaded in Ollama",
    "missing_model": "the model is not installed; run: ollama pull {tag}",
    "cloud_model": "the model runs in the cloud; choose a local model",
}


def hide_secrets(source):
    """Replace passwords, keys and tokens written in the code before it is sent to the model."""
    hidden = 0

    def hide(match):
        nonlocal hidden
        if PLACEHOLDER.match(match.group(2)):
            return match.group(0)
        hidden += 1
        start, end = match.start(2) - match.start(0), match.end(2) - match.start(0)
        return match.group(0)[:start] + "<hidden>" + match.group(0)[end:]
    return SECRET.sub(hide, source), hidden


def profile_for(lab, tag, model_digest):
    """Answers depend on these; a change means files are explained again. Device and threads only change speed.

    model_digest identifies the downloaded model version: downloading a newer version of the same model counts.
    """
    return digest(json.dumps({
        "model": tag, "model_digest": model_digest, "prompt": digest(PROMPT), "schema": SCHEMA_VERSION,
        "input": INPUT_VERSION, "checks": CHECKS_VERSION,
        **{key: lab[key] for key in ("num_ctx", "num_predict", "temperature", "seed", "repair_attempts",
                                     "long_file_tokens", "long_file_keep_tokens", "max_source_chars")},
    }, sort_keys=True))[:16]


def check_local_model(client, tag, own_models=()):
    """Raise LabError unless Ollama answers, the model is installed and runs locally, and no other model is loaded.

    Used before AI explanations and AI topics. Unlike the AI test lab, nothing is unloaded: another
    program may be using the same model. Returns the installed model entry (with its digest).
    """
    installed = client.match(client.installed(), tag)
    if installed is None:
        raise LabError(f"Model is not installed: {tag}", "missing_model")
    info = client.request("/api/show", {"model": tag}, timeout=15)
    if info.get("remote_host") or info.get("remote_model"):
        raise LabError("Cloud-backed models are not used.", "cloud_model")
    others = [m.get("name", m.get("model", "unknown")) for m in client.running()
              if not any(client.match([m], own) for own in {*own_models, tag})]
    if others:
        raise LabError("Other models are loaded: " + ", ".join(others), "busy")
    return installed


def waiting_reason(error, tag):
    """A short reason for the status, such as "Ollama is not running; open Ollama"."""
    return WAITING_REASONS[error.code].format(tag=tag) if error.code in WAITING_REASONS else str(error)


def extension_of(path):
    """Saved explanations are reused for the same contents with the same extension (the language facts use it)."""
    return Path(path).suffix.lower()


class AnalysisService:
    """AI explanations of new and changed code files, one at a time in the background.

    The scanner reports every saved change. Files of the types in ai.analyze_types wait in a queue
    kept in the database, so the queue survives a restart. One worker thread explains one file at a
    time while the service runs, with the AI lab's pipeline (ai_lab/explain and
    ai_lab/testing/runner.result_for): program facts, the model's answer, checks and at most one
    correction, so answers match the AI test lab. Results are saved by file contents, extension and
    a "profile" (model version, prompt and format versions): unchanged or duplicate files are not sent
    again. When Ollama is unavailable the worker waits, with growing pauses, and tries again.
    """

    IDLE_SECONDS = 5
    MAX_WAIT_SECONDS = 600

    def __init__(self, config_service, artifacts_service, client_factory=OllamaClient):
        self.config_service = config_service
        self.artifacts_service = artifacts_service
        self.code_steward_service = None  # set by ServiceContainer; files are explained only while it runs
        self.name_for = os.path.basename  # name shown to the model; ServiceContainer uses the path below the root
        self.database_path = artifacts_service.database_path
        self._client_factory = client_factory
        # Settings, the Ollama connection and the worker thread change together under this lock. A request
        # takes one consistent copy of them (see _connection), so a settings reload cannot mix two models.
        self._lock = threading.Lock()
        self._client = None
        self._model_digest = None
        self._own_models = set()  # models this service loaded itself; they never count as "busy"
        self._wait_until = 0.0
        self._waiting_streak = 0
        self._recent_seconds = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.state = "not started"
        self.current = None
        self.last_error = None
        self._create_tables()
        self.reload_settings()

    # Settings

    def reload_settings(self):
        """Read the ai section and the AI lab settings file it names; called again after a config reload.

        A file being explained meanwhile keeps the settings it started with: its answer is saved for
        those settings, and the file waits again for the new ones.
        """
        ai = self.config_service.get_config()["ai"]
        problem, lab, tag = None, None, None
        try:
            path = Path(os.path.expanduser(ai["settings_file"]))
            lab = load_config(path if path.is_absolute() else PROGRAM_FOLDER / path)
            lab["device"] = ai["device"]
            if ai["num_thread"] != "auto" or lab["num_thread"] != "auto":
                lab["num_thread"] = ai["num_thread"]
            tag = resolve_model(lab, ai["model"])
        except LabError as error:
            problem, lab, tag = error, None, None
        model_digest = self._remembered_digest(tag) if tag else None
        with self._lock:
            self.enabled = ai["enabled"] and problem is None
            self.types, self.max_attempts = set(ai["analyze_types"]), ai["max_attempts"]
            self.lab, self.tag, self._model_digest = lab, tag, model_digest
            self.profile = profile_for(lab, tag, model_digest) if lab else None
            self._client = None  # connect again with the new settings
            self.state = (f"off: AI settings problem: {problem}" if problem
                          else "ready to start" if self.enabled else "off")
        if problem:
            logger.error("AI analysis is off: %s", problem)
        self._wake.set()

    # Storage

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _create_tables(self):
        with self._connect() as connection:
            # profile: the settings a failed or skipped file was tried with; a new profile tries it again.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    artifact_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    force INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    queued_at TEXT NOT NULL,
                    next_try REAL NOT NULL DEFAULT 0,
                    error TEXT,
                    profile TEXT
                )
            """)
            if "profile" not in {row["name"] for row in connection.execute("PRAGMA table_info(analysis_jobs)")}:
                connection.execute("ALTER TABLE analysis_jobs ADD COLUMN profile TEXT")
            if "extension" not in {row["name"] for row in connection.execute("PRAGMA table_info(analyses)")}:
                # Explanations saved by an earlier test version, without the extension; they are made again.
                connection.execute("DROP TABLE IF EXISTS analyses")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analyses (
                    hash TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    result TEXT NOT NULL,
                    PRIMARY KEY (hash, extension, profile)
                )
            """)
            # The installed version (digest) of each model when it was last checked; part of the profile.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analysis_models (
                    tag TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS analysis_jobs_waiting ON analysis_jobs (status, queued_at)")

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.abspath(path))

    @staticmethod
    def _saved_result(connection, file_hash, extension, profile):
        row = connection.execute("SELECT result FROM analyses WHERE hash = ? AND extension = ? AND profile = ?",
                                 (file_hash, extension, profile)).fetchone()
        return json.loads(row["result"]) if row else None

    def _remembered_digest(self, tag):
        with self._connect() as connection:
            row = connection.execute("SELECT digest FROM analysis_models WHERE tag = ?", (tag,)).fetchone()
        return row["digest"] if row else None

    def _remember_digest(self, tag, model_digest):
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO analysis_models (tag, digest, checked_at) VALUES (?, ?, ?)",
                               (tag, model_digest, datetime.now(timezone.utc).isoformat()))

    # Changes reported by the scanner

    def artifact_changed(self, artifact):
        """A file was created, changed or moved: queue it unless its contents were already explained."""
        if not self.enabled:
            return
        if artifact.get("artifact_type") not in self.types or not artifact.get("hash"):
            with self._connect() as connection:
                connection.execute("DELETE FROM analysis_jobs WHERE artifact_id = ?", (artifact["id"],))
            return
        self.queue(artifact)

    def artifact_removed(self, path):
        with self._connect() as connection:
            connection.execute("DELETE FROM analysis_jobs WHERE path = ?", (self._path_key(path),))

    def queue(self, artifact, force=False):
        """Queue one file. force sends it again even when a saved result exists (POST /analysis/{id})."""
        profile = self.profile
        with self._connect() as connection:
            if not force and self._saved_result(connection, artifact["hash"], extension_of(artifact["path"]),
                                                profile) is not None:
                connection.execute("DELETE FROM analysis_jobs WHERE artifact_id = ?", (artifact["id"],))
                return False
            # A newer version replaces a waiting older one; a finished older version keeps its own result.
            connection.execute("""
                INSERT INTO analysis_jobs (artifact_id, path, hash, status, force, attempts, queued_at, next_try, error,
                                           profile)
                VALUES (?, ?, ?, 'queued', ?, 0, ?, 0, NULL, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET path = excluded.path, hash = excluded.hash, status = 'queued',
                    force = excluded.force, attempts = 0, queued_at = excluded.queued_at, next_try = 0, error = NULL,
                    profile = excluded.profile
            """, (artifact["id"], self._path_key(artifact["path"]), artifact["hash"], int(force),
                  datetime.now(timezone.utc).isoformat(), profile))
        self._wake.set()
        return True

    def queue_missing(self):
        """Queue saved files that have no result for the current profile (first start, new model or prompt).

        Files that failed or were skipped with other settings are tried again; with these settings they are not.
        """
        if not self.enabled:
            return 0
        queued, profile = 0, self.profile
        with self._connect() as connection:
            waiting = {row[0] for row in connection.execute(
                "SELECT artifact_id FROM analysis_jobs WHERE status IN ('queued', 'running') OR profile IS ?", (profile,))}
            done = {(row[0], row[1]) for row in connection.execute(
                "SELECT hash, extension FROM analyses WHERE profile = ?", (profile,))}
        for artifact in self.artifacts_service.list_artifacts()["items"]:
            if (artifact.get("artifact_type") in self.types and artifact.get("hash") and artifact["id"] not in waiting
                    and (artifact["hash"], extension_of(artifact["path"])) not in done):
                queued += self.queue(artifact)
        if queued:
            logger.info("Queued %d saved code files for AI explanations.", queued)
        return queued

    # Worker

    def start(self):
        """Start the worker thread (called when the background service starts).

        After a stop that was still waiting for a file to finish, that worker simply goes on.
        """
        with self._lock:
            self._stop.clear()
            if self._thread is not None and self._thread.is_alive():
                return
            with self._connect() as connection:
                # A file that was being explained when the program stopped is explained again.
                connection.execute("UPDATE analysis_jobs SET status = 'queued' WHERE status = 'running'")
            self._thread = threading.Thread(target=self._run, name="code-steward-analysis", daemon=True)
            self._thread.start()

    def stop(self, timeout=None):
        """Stop after the current file. Returns False when the file was still being explained after timeout:
        its answer is still saved, then the worker stops (or goes on, after a new start)."""
        with self._lock:
            self._stop.set()
            thread = self._thread
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        stopped = thread is None or not thread.is_alive()
        if not stopped:
            logger.info("AI analysis stops after the file being explained: %s", self.current)
        return stopped

    def wake(self):
        self._wake.set()

    def _run(self):
        try:
            self.queue_missing()
        except Exception:
            logger.exception("Could not queue saved files for AI explanations.")
        while True:
            with self._lock:
                if self._stop.is_set():
                    if self._thread is threading.current_thread():
                        self._thread = None  # from here a start() creates a new worker
                    return
            try:
                worked = self.step()
            except Exception as error:
                self.last_error = f"{type(error).__name__}: {error}"
                logger.exception("AI analysis step failed unexpectedly.")
                worked = False
            if not worked:
                self._wake.wait(self.IDLE_SECONDS)
                self._wake.clear()

    def _service_running(self):
        return self.code_steward_service is None or self.code_steward_service.state.value == "running"

    def step(self):
        """Explain the next waiting file. Returns True when a file was handled.

        After a start or a settings change Ollama is checked even when no file waits, so that a newer
        download of the model is noticed: files are then explained again with it.
        """
        if not self.enabled or not self._service_running() or time.time() < self._wait_until:
            return False
        with self._connect() as connection:
            job = connection.execute("""
                SELECT * FROM analysis_jobs WHERE status = 'queued' AND next_try <= ? ORDER BY queued_at LIMIT 1
            """, (time.time(),)).fetchone()
        if job is None and self._client is not None:
            return False
        request = self._connection()
        if request is None or job is None:
            return False
        job = dict(job)
        artifact = self.artifacts_service.find_artifact(job["artifact_id"])
        if artifact is None or artifact.get("hash") != job["hash"]:
            self._finish(job)  # changed or deleted since it was queued; a newer job is already waiting
            return True
        with self._connect() as connection:
            if not job["force"] and self._saved_result(connection, job["hash"], extension_of(artifact["path"]),
                                                       request.profile) is not None:
                connection.execute("DELETE FROM analysis_jobs WHERE artifact_id = ? AND hash = ?",
                                   (job["artifact_id"], job["hash"]))
                return True
            connection.execute("UPDATE analysis_jobs SET status = 'running', attempts = attempts + 1 "
                               "WHERE artifact_id = ? AND hash = ?", (job["artifact_id"], job["hash"]))
        try:
            self._explain(job, artifact, request)
        except Exception:
            with self._connect() as connection:  # try this file again later instead of leaving it "running"
                connection.execute("UPDATE analysis_jobs SET status = 'queued', next_try = ? WHERE artifact_id = ? AND hash = ?",
                                   (time.time() + 300, job["artifact_id"], job["hash"]))
            raise
        return True

    def _connection(self):
        """The settings and Ollama client for the next request, taken together; None while waiting for Ollama.

        Checked once per settings with check_local_model. A request keeps this copy from start to end.
        """
        with self._lock:
            if self._client is not None:
                return self._request()
            lab, tag, own_models = self.lab, self.tag, set(self._own_models)
        client = self._client_factory(lab)
        try:
            installed = check_local_model(client, tag, own_models)
        except LabError as error:
            self._wait_for_ollama(error)
            return None
        model_digest = installed.get("digest")
        with self._lock:
            if self.lab is not lab:
                return None  # the settings changed during the checks; the next step checks the new ones
            self._client = client
            self._own_models.add(tag)
            self._waiting_streak, self.state = 0, "ready"
            previous, updated = self._model_digest, model_digest != self._model_digest
            if updated:
                self._model_digest, self.profile = model_digest, profile_for(lab, tag, model_digest)
            request = self._request()
        logger.info("AI analysis ready: %s on %s.", tag, "CPU" if lab["device"] == "cpu" else "GPU if available")
        if updated and model_digest:
            self._remember_digest(tag, model_digest)
            if previous is not None:
                logger.warning("A new version of %s was downloaded; code files are explained again with it.", tag)
            self.queue_missing()
        return request

    def _request(self):
        """One consistent copy of the settings for a request (called with self._lock held)."""
        return SimpleNamespace(client=self._client, lab=self.lab, tag=self.tag, profile=self.profile,
                               model_digest=self._model_digest)

    def topic_model(self):
        """Client, model and model version for the organization plan's topic requests.

        The same settings and checks as for explanations (local model, installed, no other model
        loaded). Raises LabError with the reason; the plan then uses keyword hints.
        """
        with self._lock:
            enabled, lab, tag, own_models = self.enabled, self.lab, self.tag, set(self._own_models)
        if not enabled or lab is None:
            raise LabError("AI explanations are off (ai.enabled in config.yaml)", "off")
        client = self._client_factory(lab)
        installed = check_local_model(client, tag, own_models)
        with self._lock:
            self._own_models.add(tag)
        return client, tag, installed.get("digest")

    def _wait_for_ollama(self, error):
        self._client = None
        self._waiting_streak += 1
        pause = min(30 * 2 ** (self._waiting_streak - 1), self.MAX_WAIT_SECONDS)
        self._wait_until = time.time() + pause
        reason = waiting_reason(error, self.tag)
        if self.state != f"waiting: {reason}":
            logger.warning("AI analysis is waiting: %s. Trying again in %d s.", reason, pause)
        self.state, self.last_error = f"waiting: {reason}", str(error)

    def _case(self, artifact, lab):
        """The AI lab's input for one file: text, hash and facts, with written secrets hidden."""
        case = snapshot(Path(artifact["path"]), self.name_for(artifact["path"]), lab["max_source_chars"])
        if "source" in case:
            case["source"], case["hidden_secrets"] = hide_secrets(case["source"])
            if case["hidden_secrets"]:
                case["facts"] = static_facts(case["source"], extension_of(artifact["path"]))
        return case

    def _explain(self, job, artifact, request):
        case = self._case(artifact, request.lab)
        if "input_error" not in case and case["source_hash"] != job["hash"]:
            self._finish(job)  # the file changed after the scan; the scanner queues the new version
            return
        if "input_error" in case:
            self._finish(job, request, status="skipped", error=case["input_error"])
            logger.info("Not explained (%s): %s", case["input_error"], artifact["path"])
            return
        started = time.monotonic()
        self.current = artifact["path"]
        try:
            row = result_for(request.client, request.tag, case, request.lab)
        finally:
            self.current = None
        seconds = round(time.monotonic() - started, 1)
        if row["status"] == "valid":
            result = {
                "analysis": row["analysis"],
                "model": request.tag,
                "model_digest": request.model_digest,
                "profile": request.profile,
                "device": request.lab["device"],
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
                "seconds": seconds,
                "tokens_per_second": row.get("tokens_per_second"),
                "repair_used": bool((row.get("repair") or {}).get("used")),
                "filled_by_program": sorted({item["field"] for item in row.get("filled") or []}),
                "check_notes": [f"{issue['field']}: {issue['problem']}" for issue in row.get("check_issues") or []],
                "hidden_secrets": case.get("hidden_secrets", 0),
            }
            with self._connect() as connection:
                connection.execute("INSERT OR REPLACE INTO analyses (hash, extension, profile, created_at, result) "
                                   "VALUES (?, ?, ?, ?, ?)", (job["hash"], extension_of(artifact["path"]),
                                                              request.profile, result["analyzed_at"], json.dumps(result)))
            self._finish(job, request)
            self._recent_seconds = (self._recent_seconds + [seconds])[-20:]
            logger.info("Explained %s (%.1f s, %s tok/s).", artifact["path"], seconds, row.get("tokens_per_second"))
        elif row["status"] in WAIT_FOR_OLLAMA:
            with self._connect() as connection:
                connection.execute("UPDATE analysis_jobs SET status = 'queued', attempts = attempts - 1 "
                                   "WHERE artifact_id = ? AND hash = ?", (job["artifact_id"], job["hash"]))
            self._wait_for_ollama(LabError(row.get("error", ""), row["status"]))
        else:
            error = row.get("error") or row["status"]
            if job["attempts"] + 1 >= self.max_attempts or row["status"] == "invalid_output":
                # The same answer would come back (fixed seed), so a format failure is not retried.
                self._finish(job, request, status="failed", error=error)
                logger.warning("AI explanation failed for %s: %s", artifact["path"], error)
            else:
                with self._connect() as connection:
                    connection.execute("UPDATE analysis_jobs SET status = 'queued', next_try = ?, error = ? "
                                       "WHERE artifact_id = ? AND hash = ?",
                                       (time.time() + 60 * (job["attempts"] + 1), error, job["artifact_id"], job["hash"]))
            self.last_error = error

    def _finish(self, job, request=None, status=None, error=None):
        """Remove a handled job, or keep it as failed/skipped with the reason. A newer version is not touched.

        When the settings changed while the file was being explained, the answer belongs to the old
        settings, so the file waits again and is explained with the new ones.
        """
        with self._connect() as connection:
            if request is not None and request.profile != self.profile:
                connection.execute("UPDATE analysis_jobs SET status = 'queued', attempts = 0, next_try = 0, error = NULL, "
                                   "profile = ? WHERE artifact_id = ? AND hash = ?",
                                   (self.profile, job["artifact_id"], job["hash"]))
            elif status is None:
                connection.execute("DELETE FROM analysis_jobs WHERE artifact_id = ? AND hash = ?",
                                   (job["artifact_id"], job["hash"]))
            else:
                connection.execute("UPDATE analysis_jobs SET status = ?, error = ?, profile = ? "
                                   "WHERE artifact_id = ? AND hash = ?",
                                   (status, error, request.profile if request else self.profile,
                                    job["artifact_id"], job["hash"]))

    # Reading results

    def analysis_for(self, artifact):
        """The explanation of one artifact, or why there is none yet."""
        extension, profile = extension_of(artifact["path"]), self.profile
        with self._connect() as connection:
            if profile and artifact.get("hash"):
                saved = self._saved_result(connection, artifact["hash"], extension, profile)
                if saved is not None:
                    return {"status": "done", **saved}
            job = connection.execute("SELECT * FROM analysis_jobs WHERE artifact_id = ?", (artifact["id"],)).fetchone()
            older = connection.execute("SELECT result FROM analyses WHERE hash = ? AND extension = ? "
                                       "ORDER BY created_at DESC LIMIT 1", (artifact.get("hash"), extension)).fetchone()
            position = None
            if job is not None and job["status"] == "queued":
                position = connection.execute("SELECT COUNT(*) FROM analysis_jobs WHERE status = 'queued' AND queued_at <= ?",
                                              (job["queued_at"],)).fetchone()[0]
        if job is not None:
            result = {"status": job["status"], "queued_at": job["queued_at"], "error": job["error"]}
            if position is not None:
                result["position_in_queue"] = position
            if job["status"] == "running" and self.current:
                result["status"] = "being explained"
        elif not self.enabled:
            result = {"status": "off", "message": "AI analysis is off (ai.enabled in config.yaml)."}
        elif artifact.get("artifact_type") not in self.types:
            result = {"status": "not analyzed", "message": "Only these file types are explained: "
                      + ", ".join(sorted(self.types)) + "."}
        elif not artifact.get("hash"):
            result = {"status": "not analyzed", "message": "The file is larger than max_file_size_bytes."}
        else:
            result = {"status": "not queued", "message": "Not explained yet. POST /analysis/{artifact_id} queues it."}
        if older is not None:
            result["previous"] = json.loads(older["result"])  # from an earlier model or prompt version
        return result

    def status(self):
        with self._connect() as connection:
            counts = dict(connection.execute("SELECT status, COUNT(*) FROM analysis_jobs GROUP BY status").fetchall())
            done = connection.execute("SELECT COUNT(*) FROM analyses WHERE profile = ?", (self.profile,)).fetchone()[0]
        average = round(sum(self._recent_seconds) / len(self._recent_seconds), 1) if self._recent_seconds else None
        waiting = counts.get("queued", 0) + counts.get("running", 0)
        return {
            "enabled": self.enabled,
            "state": self.state,
            "model": self.tag,
            "device": self.lab["device"] if self.lab else None,
            "explaining_now": self.current,
            "waiting_files": waiting,
            "failed_files": counts.get("failed", 0),
            "skipped_files": counts.get("skipped", 0),
            "explained_contents": done,
            "average_seconds_per_file": average,
            "estimated_minutes_left": round(waiting * average / 60, 1) if average else None,
            "last_error": self.last_error,
        }
