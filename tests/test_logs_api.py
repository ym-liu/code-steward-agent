import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from fastapi import FastAPI
import uvicorn

from Services.LogsService import LogsService


class LogsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.log_path = Path(cls.temporary.name) / "service.log"
        cls.logs = LogsService(cls.log_path)
        # Load the real controller with an isolated log file, without starting
        # the production service container or touching its database/configuration.
        container = ModuleType("Services.ServiceContainer")
        container.services = SimpleNamespace(logs_service=cls.logs)
        source = Path(__file__).resolve().parents[1] / "Controllers" / "LogsController.py"
        spec = importlib.util.spec_from_file_location("logs_api_test_controller", source)
        controller = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"Services.ServiceContainer": container, spec.name: controller}):
            spec.loader.exec_module(controller)
        app = FastAPI()
        app.include_router(controller.router)
        cls.listener = socket.socket()
        cls.listener.bind(("127.0.0.1", 0))
        cls.listener.listen(10)
        cls.port = cls.listener.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="off"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.listener]}, daemon=True)
        def stop_server():
            cls.server.should_exit = True
            cls.thread.join(timeout=5)
            cls.listener.close()
        cls.addClassCleanup(stop_server)
        cls.thread.start()
        deadline = time.monotonic() + 5
        while not cls.server.started and cls.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not cls.server.started:
            raise RuntimeError("Isolated API test server did not start")

    def setUp(self):
        self.log_path.write_text("", encoding="utf-8")

    def request(self, path):
        try:
            response = urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_query_returns_real_records_and_applies_filters(self):
        self.logs.append_log("WARNING", "Scan blocked while paused")
        self.logs.append_log("INFO", "Scan completed")
        status, body = self.request("/logs?limit=1&level=WARNING&search=blocked")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["level"], "WARNING")
        self.assertIn("paused", body["items"][0]["message"])
        self.assertIn("timestamp", body["items"][0])

    def test_invalid_limits_levels_and_search_are_validation_errors(self):
        for query in ("limit=0", "limit=-1", "limit=1001", "limit=abc", "level=wrong", "search=" + "x" * 201):
            with self.subTest(query=query):
                status, body = self.request("/logs?" + query)
                self.assertEqual(status, 422)
                self.assertIn("detail", body)

    def test_empty_history_is_a_successful_empty_list(self):
        status, body = self.request("/logs")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])
        self.assertEqual(body["count"], 0)

    def test_unreadable_log_returns_a_clear_service_error(self):
        with patch.object(self.logs, "list_logs", side_effect=PermissionError("private path")):
            status, body = self.request("/logs")
        self.assertEqual(status, 503)
        self.assertEqual(body["detail"], "Application logs are temporarily unavailable.")
        self.assertNotIn("private path", json.dumps(body))

    def test_api_docs_describe_real_log_records_and_limits(self):
        _, schema = self.request("/openapi.json")
        operation = schema["paths"]["/logs"]["get"]
        limit = next(p for p in operation["parameters"] if p["name"] == "limit")
        self.assertEqual(limit["schema"]["maximum"], 1000)
        self.assertIn("LogsResponse", json.dumps(operation["responses"]["200"]))
        example = schema["components"]["schemas"]["LogsResponse"]["example"]
        self.assertIn("message", example["items"][0])
        self.assertIn("503", operation["responses"])


if __name__ == "__main__":
    unittest.main()
