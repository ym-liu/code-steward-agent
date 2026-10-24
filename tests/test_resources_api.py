"""Policy validation and read-only resource visibility through the existing API."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from Controllers.ResourceController import router, _governor
from Services.ConfigService import ConfigService, validate
from Services.ResourceGovernor import ResourceGovernor, ResourceCancelled, checkpoint


class ResourceIntegrationTests(unittest.TestCase):
    def test_resource_defaults_and_invalid_reload_retains_previous_config(self):
        self.assertEqual(validate({})["resource_control"]["pause_cpu_percent"], 80)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text("resource_control:\n  pause_cpu_percent: 75\n", encoding="utf-8")
            with patch("Services.ConfigService.CONFIG_PATH", str(path)):
                config = ConfigService()
                previous = config.get_config()
                path.write_text("resource_control:\n  pause_cpu_percent: 30\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    config.reload()
                self.assertIs(config.get_config(), previous)
                self.assertEqual(config.get("resource_control", "pause_cpu_percent"), 75)

    def test_read_only_resource_endpoint_exposes_fault_without_starting_work(self):
        app = FastAPI()
        app.include_router(router)
        calls = []
        expected = {"state": "unavailable", "work_allowed": False, "error": "sensor unavailable"}
        app.dependency_overrides[_governor] = lambda: SimpleNamespace(snapshot=lambda: calls.append("snapshot") or expected)
        messages = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": "/system/resources", "raw_path": b"/system/resources", "query_string": b"",
                 "headers": [], "client": ("127.0.0.1", 10), "server": ("127.0.0.1", 8000)}
        asyncio.run(app(scope, receive, send))
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(dict(messages[0]["headers"])[b"cache-control"], b"no-store")
        self.assertEqual(json.loads(b"".join(m.get("body", b"") for m in messages)), expected)
        self.assertEqual(calls, ["snapshot"])
        self.assertIn("/system/resources", app.openapi()["paths"])

    def test_single_core_ai_is_deferred_without_dispatch_or_self_cancellation_loop(self):
        governor = ResourceGovernor(cpu_count=1)
        dispatched = False
        with self.assertRaisesRegex(ResourceCancelled, "single-core"):
            with governor.ai_slot():
                dispatched = True
        self.assertFalse(dispatched)
        self.assertFalse(governor.snapshot()["ai_work_allowed"])
        self.assertIn("single-core", governor.snapshot()["ai_defer_reason"])

    def test_manual_service_stop_releases_busy_scan_without_closing_governor(self):
        governor = ResourceGovernor(sampler=lambda: 95, priority_setter=lambda: {"applied": True})
        governor.sample_once()
        service = SimpleNamespace(state=SimpleNamespace(value="running"))
        owner = SimpleNamespace(resource_governor=governor, code_steward_service=service)
        cancelled = threading.Event()

        def blocked_scan():
            try:
                checkpoint(owner)
            except ResourceCancelled:
                cancelled.set()

        worker = threading.Thread(target=blocked_scan)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while governor.snapshot()["waiting_checkpoints"] == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            service.state.value = "stopping"
            self.assertTrue(cancelled.wait(1), "Manual stop must cancel a CPU-waiting scan")
            self.assertNotEqual(governor.snapshot()["state"], "closing")
        finally:
            governor.stop()
            worker.join(2)
        self.assertFalse(worker.is_alive())
