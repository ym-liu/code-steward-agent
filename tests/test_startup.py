"""Startup checks use fakes and temporary SQLite files; no real container or Ollama."""
import asyncio
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI

SOURCE = Path(__file__).resolve().parents[1] / "Controllers" / "StartupController.py"
SPEC = importlib.util.spec_from_file_location("startup_under_test", SOURCE)
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)


def live_thread(alive=True):
    return SimpleNamespace(is_alive=lambda: alive)


class FakeServices:
    def __init__(self, database):
        self.system = {"state": "running", "tick_count": 2, "started_at": "2026-10-18T10:00:00+00:00",
                       "last_error": None, "waiting_changes": {"full_scan_requested": None}}
        self.scan = {"is_scanning": False, "last_full_scan_at": "2026-10-18T10:00:02+00:00",
                     "last_summary": {"errors": 0}, "roots": [{"path": "/test/watched", "available": True}]}
        self.watch = {"running": True, "mode": "events", "watched": ["/test/watched"], "missing": []}
        self.code_steward_service = SimpleNamespace(get_status=lambda: self.system, _thread=live_thread())
        self.scan_service = SimpleNamespace(get_status=lambda: self.scan, is_denied=lambda path: False,
                                            in_library=lambda path: False)
        self.artifacts_service = SimpleNamespace(database_path=database)
        self.file_event_service = SimpleNamespace(status=lambda: self.watch,
                                                  observer=SimpleNamespace(is_alive=lambda: True,
                                                                           emitters=[live_thread()]))
        self.ai = {"enabled": True}
        self.config_service = SimpleNamespace(get_config=lambda: {"ai": self.ai})
        self.analysis_service = SimpleNamespace(enabled=True, ai_settings={"base_url": "http://127.0.0.1:11434"},
                                                 tag="test:local", state="ready", _own_models=set())


class FakeClient:
    def __init__(self):
        self.models = [{"name": "test:local"}]
        self.loaded = []
        self.info = {}
        self.calls = []

    @staticmethod
    def match(models, tag):
        names = {tag, tag + ":latest" if ":" not in tag else tag}
        return next((model for model in models if model.get("name", model.get("model")) in names), None)

    def installed(self):
        self.calls.append("/api/tags")
        return self.models

    def running(self):
        self.calls.append("/api/ps")
        return self.loaded

    def request(self, path, data=None):
        self.calls.append(path)
        if path != "/api/show":
            raise AssertionError("Readiness must not invoke inference or mutate Ollama")
        return self.info


async def asgi_get(app, path):
    messages = []
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "headers": [], "server": ("test", 80), "client": ("test", 1234)}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    headers = {key.decode(): value.decode() for key, value in start["headers"]}
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], headers, body


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="startup-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.database = self.folder / "artifacts.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE artifacts (id TEXT)")
            connection.execute("INSERT INTO artifacts VALUES ('a')")
            connection.commit()
        self.services = FakeServices(str(self.database))
        self.client = FakeClient()

    def checks(self):
        response = startup.readiness(self.services)
        return response, {check["id"]: check for check in response["checks"]}

    def ai(self):
        with patch.object(startup, "_ollama_client", return_value=self.client):
            return startup.ai_readiness(self.services)

    def test_all_core_components_ready_and_database_unchanged(self):
        before = self.database.read_bytes()
        response, checks = self.checks()
        self.assertTrue(response["core_ready"])
        self.assertEqual(set(checks), {"api", "service", "storage", "watcher", "scan"})
        self.assertIn("File records: 1", checks["storage"]["detail"])
        self.assertEqual(self.database.read_bytes(), before)

    def test_missing_database_is_not_created(self):
        missing = self.folder / "missing.sqlite3"
        self.services.artifacts_service.database_path = str(missing)
        response, checks = self.checks()
        self.assertFalse(response["core_ready"])
        self.assertEqual(checks["storage"]["state"], "error")
        self.assertFalse(missing.exists())

    def test_invalid_database_does_not_hide_other_components(self):
        self.database.write_text("not a sqlite database", encoding="utf-8")
        response, checks = self.checks()
        self.assertEqual(checks["storage"]["state"], "error")
        self.assertEqual(checks["watcher"]["state"], "ready")
        self.assertFalse(response["core_ready"])

    def test_paused_service_requires_action(self):
        self.services.system["state"] = "paused"
        response, checks = self.checks()
        self.assertEqual(checks["service"]["state"], "error")
        self.assertIn("resume", checks["service"]["action"])
        self.assertFalse(response["core_ready"])

    def test_service_first_tick_and_dead_thread(self):
        self.services.system["tick_count"] = 0
        self.assertEqual(self.checks()[1]["service"]["state"], "pending")
        self.services.code_steward_service._thread = live_thread(False)
        self.assertEqual(self.checks()[1]["service"]["state"], "error")

    def test_cpu_deferral_is_pending_with_reason_and_automatic_resume(self):
        self.services.system["waiting_changes"] = {"full_scan_requested": True}
        self.services.resource_governor = SimpleNamespace(
            can_work=lambda: False, snapshot=lambda: {"state": "paused"})
        response, checks = self.checks()
        self.assertFalse(response["core_ready"])
        self.assertEqual(checks["scan"]["state"], "pending")
        self.assertTrue(checks["scan"]["deferred_by_resources"])
        self.assertIn("CPU resources", checks["scan"]["detail"])
        self.assertIn("automatically", checks["scan"]["action"])

    def test_background_error_is_visible(self):
        self.services.system["last_error"] = "database is locked"
        checks = self.checks()[1]
        for identifier in ("service", "scan"):
            self.assertEqual(checks[identifier]["state"], "error")
            self.assertIn("database is locked", checks[identifier]["detail"])

    def test_queued_full_scan_cannot_use_previous_completion(self):
        self.services.system["waiting_changes"]["full_scan_requested"] = "settings-scan"
        response, checks = self.checks()
        self.assertEqual(checks["scan"]["state"], "pending")
        self.assertFalse(response["core_ready"])

    def test_scan_from_previous_service_start_is_pending(self):
        self.services.scan["last_full_scan_at"] = "2026-10-18T09:59:59+00:00"
        self.assertEqual(self.checks()[1]["scan"]["state"], "pending")

    def test_current_scan_and_file_errors(self):
        self.services.scan["is_scanning"] = True
        self.assertEqual(self.checks()[1]["scan"]["state"], "pending")
        self.services.scan["is_scanning"] = False
        self.services.scan["last_summary"]["errors"] = 3
        check = self.checks()[1]["scan"]
        self.assertEqual(check["state"], "error")
        self.assertIn("3", check["detail"])

    def test_missing_and_excluded_roots_are_errors(self):
        self.services.scan["roots"][0]["available"] = False
        checks = self.checks()[1]
        self.assertEqual(checks["scan"]["state"], "error")
        self.assertEqual(checks["watcher"]["state"], "error")
        self.services.scan["roots"][0]["available"] = True
        self.services.scan_service.is_denied = lambda path: True
        self.assertEqual(self.checks()[1]["scan"]["state"], "error")
        self.services.scan["roots"] = []
        self.assertEqual(self.checks()[1]["watcher"]["state"], "error")

    def test_dead_watch_observer_and_emitter(self):
        observer = self.services.file_event_service.observer
        observer.is_alive = lambda: False
        self.assertEqual(self.checks()[1]["watcher"]["state"], "error")
        observer.is_alive = lambda: True
        observer.emitters = [live_thread(False)]
        self.assertEqual(self.checks()[1]["watcher"]["state"], "error")

    def test_disabled_ai_is_intentionally_skipped_without_network(self):
        self.services.ai["enabled"] = False
        self.services.analysis_service.enabled = False
        self.services.analysis_service.ai_settings = None
        with patch.object(startup, "_ollama_client") as factory:
            self.assertEqual(startup.ai_readiness(self.services)["state"], "skipped")
            factory.assert_not_called()

    def test_broken_ai_settings_are_an_error_not_skipped(self):
        self.services.analysis_service.enabled = False
        self.services.analysis_service.state = "off: AI settings problem: missing file"
        check = self.ai()
        self.assertEqual(check["state"], "error")
        self.assertIn("missing file", check["detail"])

    def test_missing_model_suggests_explicit_download_without_downloading(self):
        self.client.models = []
        check = self.ai()
        self.assertEqual(check["state"], "error")
        self.assertIn("ollama pull test:local", check["action"])
        self.assertEqual(self.client.calls, ["/api/tags"])

    def test_cloud_model_and_other_loaded_models_fail(self):
        self.client.info = {"remote_model": "cloud"}
        self.assertEqual(self.ai()["state"], "error")
        self.client.info = {}
        self.client.loaded = [{"name": "unrelated:local"}]
        self.assertIn("unrelated:local", self.ai()["detail"])

    def test_ready_ai_is_a_fresh_probe_without_claiming_inference_done(self):
        self.services.analysis_service.state = "waiting: stale cached state"
        check = self.ai()
        self.assertEqual(check["state"], "ready")
        self.assertIn("the model loads on the first inference", check["detail"])
        self.assertEqual(self.client.calls, ["/api/tags", "/api/show", "/api/ps"])

    def test_own_loaded_models_match_existing_busy_policy(self):
        self.services.analysis_service._own_models = {"previous:local"}
        self.client.loaded = [{"name": "previous:local"}, {"name": "test:local"}]
        self.assertEqual(self.ai()["state"], "ready")

    def test_connection_error_has_clear_action(self):
        error = RuntimeError("Cannot reach Ollama")
        error.code = "connection_error"
        with patch.object(startup, "_ollama_client", side_effect=error):
            check = startup.ai_readiness(self.services)
        self.assertEqual(check["state"], "error")
        self.assertIn("ollama serve", check["action"])

    def test_probe_never_uses_long_inference_timeout(self):
        calls = []

        class FakeOllama:
            def __init__(self, config):
                self.config = config

            def request(self, path, data=None, timeout=None, on_chunk=None):
                calls.append(timeout)
                return {}

        module = ModuleType("ai.explain.ollama_client")
        module.OllamaClient = FakeOllama
        with patch.dict(sys.modules, {"ai.explain.ollama_client": module}):
            client = startup._ollama_client({"timeout_seconds": 1800})
            client.request("/api/show", timeout=15)
            client.request("/api/tags", timeout=5)
            client.request("/api/ps", timeout=1)
            client.request("/api/tags")
        self.assertEqual(calls, [4, 4, 1, 4])

    def test_explicit_routes_deliver_only_expected_assets_with_no_store(self):
        app = FastAPI()
        app.include_router(startup.router)
        app.dependency_overrides[startup._services] = lambda: self.services
        expected = {"index.html": b"<!doctype html><title>Startup test</title>",
                    "startup.css": b"body{color:green}", "startup.js": b"console.log('local');"}
        for name, content in expected.items():
            (self.folder / name).write_bytes(content)
        with patch.object(startup, "ASSET_ROOT", self.folder), patch.object(startup, "_ollama_client", return_value=self.client):
            for path, filename in (("/startup", "index.html"), ("/startup/", "index.html"),
                                   ("/startup/assets/startup.css", "startup.css"), ("/startup/assets/startup.js", "startup.js")):
                status, headers, body = asyncio.run(asgi_get(app, path))
                self.assertEqual(status, 200)
                self.assertEqual(body, expected[filename])
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
            for path in ("/startup/status", "/startup/ai"):
                status, headers, body = asyncio.run(asgi_get(app, path))
                self.assertEqual(status, 200)
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertIsInstance(json.loads(body), dict)
            for path in ("/startup/assets/secret.txt", "/startup/assets/../index.html", "/startup/assets/../../config.yaml"):
                self.assertEqual(asyncio.run(asgi_get(app, path))[0], 404)


if __name__ == "__main__":
    unittest.main()
