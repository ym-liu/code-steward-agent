"""The REST API end to end: a real server process with its own settings, database, log and folder."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT = Path(__file__).resolve().parents[1]
SERVER = "import sys, uvicorn; uvicorn.run('CodeSteward:app', host='127.0.0.1', port=int(sys.argv[1]), log_level='warning')"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Server:
    """Starts `uvicorn CodeSteward:app` with CODE_STEWARD_CONFIG pointing at a temporary config file."""

    def __init__(self, base, injection=False, ai_settings=None):
        self.base = base
        self.root = base / "scripts"
        self.root.mkdir(exist_ok=True)
        self.config = base / "config.yaml"
        self.log = base / "service.log"
        self.ai_settings = ai_settings  # an AI lab settings file; None keeps AI explanations off
        self.write_config(injection)
        self.port = free_port()
        self.process = subprocess.Popen([sys.executable, "-c", SERVER, str(self.port)], cwd=PROJECT,
                                        env={**os.environ, "CODE_STEWARD_CONFIG": str(self.config)},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if self.call("GET", "/system/health")[0] == 200:
                    return
            except (URLError, ConnectionError):
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("The test server did not start.")

    def write_config(self, injection=False, text=None):
        self.config.write_text(text if text is not None else (
            "service:\n  tick_seconds: 0.5\n"
            f"scanning:\n  root_paths:\n    - '{self.root}'\n"
            "events:\n  quiet_seconds: 0.2\n  max_wait_seconds: 2\n"
            f"storage:\n  database_path: '{self.base / 'artifacts.sqlite3'}'\n  log_path: '{self.log}'\n"
            "github:\n  token: 'ghp_not_a_real_token'\n"
            f"testing:\n  allow_event_injection: {'true' if injection else 'false'}\n"
            + (f"ai:\n  enabled: true\n  model: fixture\n  settings_file: '{self.ai_settings}'\n" if self.ai_settings
               else "ai:\n  enabled: false\n")), encoding="utf-8")

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def page(self, path):
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return response.headers.get("Content-Type", ""), response.read().decode("utf-8")

    def wait_for(self, path, condition, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, value = self.call("GET", path)
            if status == 200 and condition(value):
                return value
            time.sleep(0.1)
        raise AssertionError(f"{path} did not reach the expected state: {value}")

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        base = Path(temporary.name)
        (base / "scripts").mkdir()
        for name, text in (("tool.py", "print(1)\n"), ("copy_of_tool.py", "print(1)\n"), ("README.md", "# Notes\n"),
                           (".env", "TOKEN=replace-me\n")):
            (base / "scripts" / name).write_text(text, encoding="utf-8")
        cls.server = Server(base)
        cls.addClassCleanup(cls.server.stop)
        cls.server.wait_for("/artifacts", lambda value: value["total"] == 4)

    def call(self, method, path, body=None):
        return self.server.call(method, path, body)

    def test_the_service_starts_with_the_api_and_registers_every_file_type(self):
        status, value = self.call("GET", "/system/status")
        self.assertEqual((status, value["state"]), (200, "running"))
        types = {Path(item["path"]).name: item["artifact_type"] for item in self.call("GET", "/artifacts")[1]["items"]}
        self.assertEqual(types, {"tool.py": "script", "copy_of_tool.py": "script", "README.md": "documentation",
                                 ".env": "data"})

    def test_artifact_lists_are_paged_and_filtered(self):
        page = self.call("GET", "/artifacts?limit=2")[1]
        self.assertEqual((page["count"], page["total"], page["limit"], page["offset"]), (2, 4, 2, 0))
        documents = self.call("GET", "/artifacts?artifact_type=documentation")[1]
        self.assertEqual([Path(item["path"]).name for item in documents["items"]], ["README.md"])
        sensitive = self.call("GET", "/artifacts?sensitive=true")[1]
        self.assertEqual([Path(item["path"]).name for item in sensitive["items"]], [".env"])
        self.assertEqual(self.call("GET", "/artifacts?artifact_type=not-a-type")[0], 422)

    def test_unknown_ids_are_404_and_exact_duplicates_are_listed(self):
        self.assertEqual(self.call("GET", "/artifacts/not-a-real-id"), (404, {"detail": "Artifact not found."}))
        items = self.call("GET", "/artifacts?path_contains=tool.py")[1]["items"]
        tool = next(item for item in items if item["name"] == "tool.py")
        status, detail = self.call("GET", f"/artifacts/{tool['id']}")
        self.assertEqual(status, 200)
        self.assertEqual([Path(path).name for path in detail["same_content_as"]], ["copy_of_tool.py"])

    def test_refused_controls_answer_409_and_blocked_scans_are_logged(self):
        self.assertEqual(self.call("POST", "/system/start")[0], 409)
        self.assertEqual(self.call("POST", "/system/pause"), (200, {"message": "Service paused.", "state": "paused"}))
        try:
            self.assertEqual(self.call("POST", "/system/pause")[0], 409)
            status, value = self.call("POST", "/scan/trigger")
            self.assertEqual((status, value["state"]), (409, "paused"))
            logs = self.call("GET", "/logs?level=WARNING&search=Scan%20blocked")[1]
            self.assertGreaterEqual(logs["count"], 1)
        finally:
            self.assertEqual(self.call("POST", "/system/resume")[0], 200)
        self.assertEqual(self.call("POST", "/system/resume")[0], 409)

    def test_a_scan_request_is_queued_and_finishes_in_the_background(self):
        status, value = self.call("POST", "/scan/trigger")
        self.assertEqual((status, value["message"]), (202, "Full scan queued."))
        self.server.wait_for("/scan/status", lambda value: (value["last_summary"] or {}).get("source") == "manual-scan")

    def test_settings_hide_secrets_and_an_invalid_reload_keeps_the_old_settings(self):
        settings = self.call("GET", "/config")[1]
        self.assertEqual(settings["github"]["token"], "***")
        self.assertEqual(settings["scanning"]["scan_interval_seconds"], 300)
        self.server.write_config(text="scanning: [broken\n")
        try:
            status, value = self.call("POST", "/config/reload")
            self.assertEqual(status, 422)
            self.assertIn("previous settings are still in use", value["detail"])
            self.assertEqual(self.call("GET", "/config")[1], settings)
        finally:
            self.server.write_config()
            self.assertEqual(self.call("POST", "/config/reload")[0], 200)

    def test_event_injection_is_off_by_default_and_follows_the_normal_path_when_on(self):
        event = {"event_type": "created", "path": str(self.server.root / "new_tool.py")}
        self.assertEqual(self.call("POST", "/events/inject", event)[0], 403)
        self.server.write_config(injection=True)
        try:
            self.assertEqual(self.call("POST", "/config/reload")[0], 200)
            (self.server.root / "new_tool.py").write_text("print('new')\n", encoding="utf-8")
            status, value = self.call("POST", "/events/inject", event)
            self.assertEqual((status, value["queued"]), (202, True))
            self.server.wait_for("/artifacts?path_contains=new_tool.py", lambda value: value["total"] == 1)
            outside = {"event_type": "created", "path": str(self.server.base / "elsewhere.py")}
            self.assertEqual(self.call("POST", "/events/inject", outside)[1]["queued"], False)
        finally:
            (self.server.root / "new_tool.py").unlink(missing_ok=True)
            self.server.write_config()
            self.call("POST", "/config/reload")
            self.server.wait_for("/artifacts", lambda value: value["total"] == 4)

    def test_ai_explanations_report_that_they_are_off(self):
        self.assertEqual(self.call("GET", "/analysis/status")[1]["enabled"], False)
        tool = self.call("GET", "/artifacts?path_contains=tool.py&artifact_type=script")[1]["items"][0]
        self.assertEqual(self.call("GET", f"/analysis/{tool['id']}")[1]["status"], "off")
        self.assertEqual(self.call("POST", f"/analysis/{tool['id']}")[0], 409)

    def test_an_organization_plan_without_ai_uses_keyword_hints_and_has_a_page(self):
        self.assertEqual(self.call("POST", "/organization/plan")[0], 202)
        self.server.wait_for("/organization/status", lambda value: value["state"] == "ready")
        status, plan = self.call("GET", "/organization/plan")
        self.assertEqual((status, plan["mode"], plan["topics_from"]),
                         (200, "dry-run", "keyword hints only (AI explanations are off)"))
        self.assertEqual(plan["summary"]["files"], 4)
        kind, page = self.server.page("/organization/plan/page")
        self.assertTrue(kind.startswith("text/html"))
        self.assertIn("Nothing was copied, moved or deleted", page)
        topics = [topic["id"] for topic in self.call("GET", "/organization/topics")[1]["topics"]]
        self.assertEqual(topics[-1], "other")

    def test_metrics_report_the_agent_process(self):
        self.call("GET", "/system/metrics")
        status, value = self.call("GET", "/system/metrics")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(value["threads"], 1)
        self.assertIsNotNone(value["cpu_percent_since_last_call"])
        self.assertEqual(value["service_state"], "running")


@unittest.skipIf(os.name == "nt", "Windows has no graceful terminate signal for a child process")
class AnalysisApiTests(unittest.TestCase):
    """AI explanations through the API, with the AI lab's fake Ollama server instead of a model."""

    @classmethod
    def setUpClass(cls):
        from tests.test_analysis import FakeOllama
        from ai_lab.common import SETTINGS
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        base = Path(temporary.name)
        cls.ollama = FakeOllama(cls.addClassCleanup)
        lab = json.loads((SETTINGS / "default.json").read_text(encoding="utf-8"))
        lab.update(base_url=cls.ollama.url, models={"fixture": "fixture:latest"}, default_model="fixture")
        (base / "lab.json").write_text(json.dumps(lab), encoding="utf-8")
        (base / "scripts").mkdir()
        (base / "scripts" / "tool.py").write_text("print(1)\n", encoding="utf-8")
        (base / "scripts" / "README.md").write_text("# Notes\n", encoding="utf-8")
        cls.server = Server(base, ai_settings=base / "lab.json")
        cls.addClassCleanup(cls.server.stop)
        items = cls.server.wait_for("/artifacts", lambda value: value["total"] == 2)["items"]
        cls.ids = {Path(item["path"]).name: item["id"] for item in items}

    def call(self, method, path):
        return self.server.call(method, path)

    def test_a_code_file_is_explained_and_can_be_explained_again(self):
        result = self.server.wait_for(f"/analysis/{self.ids['tool.py']}", lambda value: value["status"] == "done")
        self.assertEqual((result["analysis"]["purpose"], result["model"]), ("Print a value", "fixture:latest"))
        status = self.call("GET", "/analysis/status")[1]
        self.assertEqual((status["enabled"], status["state"], status["waiting_files"]), (True, "ready", 0))
        chats = self.ollama.state["chat_count"]
        self.assertEqual(self.call("POST", f"/analysis/{self.ids['tool.py']}")[0], 202)
        self.server.wait_for("/analysis/status", lambda value: value["waiting_files"] == 0)
        self.assertEqual(self.ollama.state["chat_count"], chats + 1)

    def test_the_organization_plan_asks_the_model_for_topics(self):
        self.server.wait_for("/analysis/status", lambda value: value["waiting_files"] == 0)
        self.ollama.state["outputs"] = [json.dumps({"reason": "Prints a value.", "topic": "developer-tools",
                                                    "folder_name": "printer"})]
        self.assertEqual(self.call("POST", "/organization/plan")[0], 202)
        self.server.wait_for("/organization/status", lambda value: value["state"] == "ready")
        plan = self.call("GET", "/organization/plan")[1]
        self.assertEqual((plan["topics_from"], plan["model"]), ("AI, checked by keyword hints", "fixture:latest"))
        self.assertEqual([unit["ai_topic"] for unit in plan["units"]], ["developer-tools"])

    def test_other_file_types_and_unknown_ids_are_refused(self):
        self.assertEqual(self.call("GET", f"/analysis/{self.ids['README.md']}")[1]["status"], "not analyzed")
        self.assertEqual(self.call("POST", f"/analysis/{self.ids['README.md']}")[0], 409)
        self.assertEqual(self.call("GET", "/analysis/not-a-real-id")[0], 404)


class ShutdownTests(unittest.TestCase):
    def test_stopping_the_server_stops_the_background_service_cleanly(self):
        with tempfile.TemporaryDirectory() as folder:
            server = Server(Path(folder))
            try:
                server.wait_for("/system/status", lambda value: value["state"] == "running")
            finally:
                server.process.send_signal(signal.SIGTERM)
                # uvicorn shuts down, then passes the signal on, so the exit code reports SIGTERM.
                self.assertIn(server.process.wait(timeout=20), (0, -signal.SIGTERM))
            log = server.log.read_text(encoding="utf-8")
            self.assertIn("Service shutting down", log)
            self.assertIn("File event service stopped", log)


if __name__ == "__main__":
    unittest.main()
