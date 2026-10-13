"""AI explanations of new and changed files: queue, saved results, waiting for Ollama and restarts.

A fake Ollama server from the AI lab tests answers every request, so no real model is used.
"""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_lab.common import SETTINGS
from Services.AnalysisService import AnalysisService, hide_secrets
from Services.ArtifactsService import ArtifactsService
from Services.ConfigService import validate
from Services.EventAggregator import EventAggregator
from Services.FileEventService import FileEventService
from Services.ScanService import ScanService
from ai_lab.testing.runner import result_for
from service import CodeStewardService
from tests.test_ai_lab import FixtureHandler
from tests.test_file_events import wait_until


class FakeOllama:
    """Answers like Ollama with one fixed explanation: purpose "Print a value"."""

    def __init__(self, add_cleanup):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.state = self.server.state = {"installed": ["fixture:latest"], "running": [], "vram": 0,
                                          "requests": [], "outputs": [], "chat_count": 0}
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
        add_cleanup(stop)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def sent(self):
        return [body for path, body in self.state["requests"] if path == "/api/chat"]


class Settings:
    def __init__(self, **sections):
        self.config = validate(sections)

    def get_config(self):
        return self.config

    def get(self, *keys, default=None):
        value = self.config
        for key in keys:
            value = value.get(key, default) if isinstance(value, dict) else default
        return value


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "scripts"
        self.root.mkdir()
        self.ollama = FakeOllama(self.addCleanup)
        self.lab_file = self.base / "lab-settings.json"
        self.write_lab_settings()
        self.settings = Settings(scanning={"root_paths": [str(self.root)]},
                                 ai={"model": "fixture", "settings_file": str(self.lab_file)})
        self.artifacts = ArtifactsService(str(self.base / "artifacts.sqlite3"))
        self.scanner = ScanService(self.settings, None, self.artifacts)
        self.analysis = AnalysisService(self.settings, self.artifacts)
        self.scanner.listeners.append(self.analysis)

    def write_lab_settings(self, **changes):
        lab = json.loads((SETTINGS / "default.json").read_text(encoding="utf-8"))
        lab.update({"base_url": self.ollama.url, "models": {"fixture": "fixture:latest", "other": "fixture2:latest"},
                    "default_model": "fixture", **changes})
        self.lab_file.write_text(json.dumps(lab), encoding="utf-8")

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def scan(self):
        return self.scanner.process(full_scan="periodic-scan")

    def work(self):
        while self.analysis.step():
            pass

    def result(self, name):
        artifact = next(a for a in self.artifacts.list_artifacts()["items"] if Path(a["path"]).name == name)
        return self.analysis.analysis_for(artifact)

    def slow_model(self):
        """Patch in a model that waits for release.set() before it answers, like a long request."""
        entered, release = threading.Event(), threading.Event()

        def answer(client, tag, case, lab):
            entered.set()
            release.wait(5)
            return result_for(client, tag, case, lab)
        patcher = patch("Services.AnalysisService.result_for", answer)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(release.set)
        return entered, release

    def test_a_new_code_file_is_explained_once_and_saved(self):
        self.write("tool.py", "print(1)\n")
        self.scan()
        self.assertEqual(self.result("tool.py")["status"], "queued")
        self.work()
        result = self.result("tool.py")
        self.assertEqual((result["status"], result["analysis"]["purpose"], result["model"]),
                         ("done", "Print a value", "fixture:latest"))
        self.assertEqual(self.ollama.state["chat_count"], 1)
        self.assertIn("FILE: tool.py", self.ollama.sent()[0]["messages"][-1]["content"])
        self.assertEqual(self.analysis.status()["waiting_files"], 0)

    def test_unchanged_and_duplicate_contents_are_not_sent_again(self):
        self.write("tool.py", "print(1)\n")
        self.write("copy_of_tool.py", "print(1)\n")
        self.scan()
        self.work()
        self.scan()
        self.work()
        self.assertEqual(self.ollama.state["chat_count"], 1)
        self.assertEqual(self.result("copy_of_tool.py")["status"], "done")

    def test_only_the_newest_version_of_a_changed_file_is_sent(self):
        self.write("tool.py", "print(1)\n")
        self.scan()
        self.write("tool.py", "print('a newer version')\n")
        self.scan()
        self.work()
        self.assertEqual(self.ollama.state["chat_count"], 1)
        self.assertIn("a newer version", self.ollama.sent()[0]["messages"][-1]["content"])

    def test_only_code_files_are_queued_and_deleted_files_leave_the_queue(self):
        self.write("notes.md", "# Notes\n")
        self.write("prices.csv", "a,b\n1,2\n")
        self.write("tool.py", "print(1)\n")
        self.scan()
        self.assertEqual(self.result("notes.md")["status"], "not analyzed")
        (self.root / "tool.py").unlink()
        self.scan()
        self.work()
        self.assertEqual(self.analysis.status()["waiting_files"], 0)
        self.assertEqual(self.ollama.state["chat_count"], 0)

    def test_a_file_interrupted_by_a_stop_is_explained_after_a_restart(self):
        self.write("tool.py", "print(1)\n")
        self.scan()
        with sqlite3.connect(self.artifacts.database_path) as connection:
            connection.execute("UPDATE analysis_jobs SET status = 'running'")
        restarted = AnalysisService(self.settings, self.artifacts)
        restarted.start()
        self.addCleanup(restarted.stop, 5)
        artifact = self.artifacts.list_artifacts()["items"][0]
        self.assertTrue(wait_until(lambda: restarted.analysis_for(artifact)["status"] == "done"))

    def test_without_ollama_files_wait_and_the_reason_is_logged_once(self):
        self.write_lab_settings(base_url="http://127.0.0.1:9")  # nothing listens there
        self.analysis.reload_settings()
        self.write("tool.py", "print(1)\n")
        self.scan()
        with self.assertLogs("code-steward.analysis", level="WARNING") as logs:
            self.assertFalse(self.analysis.step())
            self.analysis._wait_until = 0  # as if the pause were over
            self.assertFalse(self.analysis.step())
        self.assertEqual(len(logs.output), 1)
        self.assertIn("Ollama is not running", self.analysis.status()["state"])
        self.assertEqual(self.result("tool.py")["status"], "queued")

    def test_a_missing_model_or_another_loaded_model_makes_files_wait_with_a_clear_reason(self):
        self.write("tool.py", "print(1)\n")
        self.scan()
        self.ollama.state["installed"] = []
        with self.assertLogs("code-steward.analysis", level="WARNING"):
            self.assertFalse(self.analysis.step())
        self.assertIn("ollama pull fixture:latest", self.analysis.status()["state"])
        self.ollama.state.update(installed=["fixture:latest"], running=["someone-else:latest"])
        self.analysis._wait_until = 0
        with self.assertLogs("code-steward.analysis", level="WARNING"):
            self.assertFalse(self.analysis.step())
        self.assertIn("another model is loaded", self.analysis.status()["state"])
        self.ollama.state["running"] = []
        self.analysis._wait_until = 0
        self.work()
        self.assertEqual(self.result("tool.py")["status"], "done")

    def test_an_unreadable_answer_is_marked_failed_without_asking_again(self):
        self.ollama.state["outputs"] = ["not json"] * 3
        self.write("tool.py", "print(1)\n")
        self.scan()
        with self.assertLogs("code-steward.analysis", level="WARNING"):
            self.work()
        result = self.result("tool.py")
        self.assertEqual(result["status"], "failed")
        self.assertIn("Invalid JSON", result["error"])
        self.assertEqual(self.ollama.state["chat_count"], 1)

    def test_written_passwords_are_hidden_before_the_code_is_sent(self):
        self.write("send.py", 'password = "hunter2hunter"\nprint(len(password))\n')
        self.scan()
        self.work()
        sent = json.dumps(self.ollama.sent())
        self.assertNotIn("hunter2hunter", sent)
        self.assertIn("<hidden>", sent)
        self.assertEqual(self.result("send.py")["hidden_secrets"], 1)
        self.assertEqual(hide_secrets('token = "change-me"')[1], 0)

    def test_a_new_model_explains_files_again_and_shows_the_previous_answer_meanwhile(self):
        self.write("tool.py", "print(1)\n")
        self.scan()
        self.work()
        self.settings.config["ai"]["model"] = "other"
        self.ollama.state["installed"].append("fixture2:latest")
        self.analysis.reload_settings()
        self.assertEqual(self.analysis.queue_missing(), 1)
        result = self.result("tool.py")
        self.assertEqual((result["status"], result["previous"]["model"]), ("queued", "fixture:latest"))
        self.work()
        self.assertEqual(self.result("tool.py")["model"], "fixture2:latest")

    def test_a_settings_change_during_a_request_keeps_each_answer_with_its_own_model(self):
        self.ollama.state["installed"].append("fixture2:latest")
        self.write("tool.py", "print(1)\n")
        self.scan()
        entered, release = self.slow_model()
        worker = threading.Thread(target=self.analysis.step)
        worker.start()
        self.assertTrue(entered.wait(5))
        self.settings.config["ai"]["model"] = "other"
        self.analysis.reload_settings()
        release.set()
        worker.join(5)
        result = self.result("tool.py")
        self.assertEqual((result["status"], result["previous"]["model"]), ("queued", "fixture:latest"))
        self.work()
        result = self.result("tool.py")
        self.assertEqual((result["status"], result["model"]), ("done", "fixture2:latest"))
        self.assertEqual([body["model"] for body in self.ollama.sent()], ["fixture:latest", "fixture2:latest"])

    def test_a_restart_right_after_a_slow_stop_keeps_explaining_files(self):
        self.write("tool.py", "print(1)\n")
        self.scan()
        entered, release = self.slow_model()
        self.analysis.start()
        self.addCleanup(self.analysis.stop, 5)
        self.assertTrue(entered.wait(5))
        self.assertFalse(self.analysis.stop(timeout=0.05))  # still explaining tool.py
        self.analysis.start()
        release.set()
        self.assertTrue(wait_until(lambda: self.result("tool.py")["status"] == "done"))
        self.write("next.py", "print(2)\n")
        self.scan()
        self.assertTrue(wait_until(lambda: self.result("next.py")["status"] == "done"))

    def test_the_same_contents_with_another_extension_are_explained_separately(self):
        for name in ("tool.py", "copy.py", "tool.js"):
            self.write(name, "print(1)\n")
        self.scan()
        self.work()
        self.assertEqual(self.ollama.state["chat_count"], 2)  # copy.py reuses the answer for tool.py
        self.assertTrue(any("FILE: tool.js" in body["messages"][-1]["content"] for body in self.ollama.sent()))
        self.assertEqual({self.result(name)["status"] for name in ("tool.py", "copy.py", "tool.js")}, {"done"})

    def test_a_newer_download_of_the_same_model_explains_files_again(self):
        self.write("tool.py", "print(1)\n")
        self.scan()
        self.work()
        self.assertEqual(self.result("tool.py")["model_digest"], "sha256:fixture:latest")
        self.ollama.state["digests"] = {"fixture:latest": "sha256:newer"}
        restarted = AnalysisService(self.settings, self.artifacts)
        self.assertEqual(restarted.analysis_for(self.artifacts.list_artifacts()["items"][0])["status"], "done")
        with self.assertLogs("code-steward.analysis", level="WARNING"):
            restarted.step()  # checks Ollama once after a start, even with no waiting file
        while restarted.step():
            pass
        result = restarted.analysis_for(self.artifacts.list_artifacts()["items"][0])
        self.assertEqual((result["status"], result["model_digest"]), ("done", "sha256:newer"))
        self.assertEqual(self.ollama.state["chat_count"], 2)

    def test_files_that_failed_with_one_model_are_tried_again_with_another(self):
        self.ollama.state["outputs"] = ["not json"] * 3
        self.write("tool.py", "print(1)\n")
        self.scan()
        with self.assertLogs("code-steward.analysis", level="WARNING"):
            self.work()
        self.assertEqual(self.result("tool.py")["status"], "failed")
        self.assertEqual(self.analysis.queue_missing(), 0)  # the same settings would fail the same way
        self.ollama.state.update(outputs=[], installed=["fixture:latest", "fixture2:latest"])
        self.settings.config["ai"]["model"] = "other"
        self.analysis.reload_settings()
        self.assertEqual(self.analysis.queue_missing(), 1)
        self.work()
        self.assertEqual((self.result("tool.py")["status"], self.result("tool.py")["model"]), ("done", "fixture2:latest"))

    def test_nothing_is_sent_while_the_service_is_paused(self):
        self.write("tool.py", "print(1)\n")
        self.scan()
        self.analysis.code_steward_service = SimpleNamespace(state=SimpleNamespace(value="paused"))
        self.assertFalse(self.analysis.step())
        self.assertEqual(self.ollama.state["chat_count"], 0)

    def test_a_saved_file_is_explained_in_the_background_while_the_service_runs(self):
        self.settings.config["service"]["tick_seconds"] = 0.05
        aggregator = EventAggregator(quiet_seconds=0, max_wait_seconds=0)
        watcher = FileEventService(aggregator, self.scanner)
        service = CodeStewardService(scan_service=self.scanner, aggregator=aggregator, analysis_service=self.analysis)
        self.scanner.code_steward_service = self.analysis.code_steward_service = service
        self.addCleanup(lambda: (service.stop(), service.wait_stopped(5)))
        service.start()
        path = self.write("tool.py", "print(1)\n")
        watcher.event_handler.handle("created", str(path))
        self.assertTrue(wait_until(lambda: any(a["name"] == "tool.py" for a in self.artifacts.list_artifacts()["items"])))
        self.assertTrue(wait_until(lambda: self.result("tool.py")["status"] == "done"))


if __name__ == "__main__":
    unittest.main()
