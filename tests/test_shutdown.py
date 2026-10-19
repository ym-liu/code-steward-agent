"""Shutdown tests use fake workers and ASGI messages; no live app or Ollama."""
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

from Controllers import ShutdownController as controller
from Services.ShutdownCoordinator import ShutdownAdmissionMiddleware, ShutdownCoordinator
from launch import create_server


async def request(app, path, *, method="GET", payload=None, headers=None, client="127.0.0.1", messages=None):
    messages = [] if messages is None else messages
    raw = b"" if payload is None else json.dumps(payload).encode()
    values = {"host": "127.0.0.1:8000", **(headers or {})}
    if payload is not None:
        values["content-type"] = "application/json"
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in values.items()],
             "server": ("127.0.0.1", 8000), "client": (client, 1234)}

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return start["status"], {key.decode(): value.decode() for key, value in start["headers"]}, body


class Runtime:
    def __init__(self):
        self.service_gate, self.ai_gate = threading.Event(), threading.Event()
        self.service_gate.set()
        self.ai_gate.set()
        self.service_stop = threading.Event()
        self.ai_stop = threading.Event()
        self.calls = []
        self.observer_alive = True
        self.watch_running = True
        self.scan_running = False
        self.watcher_error = False
        self.service_thread_alive = True
        self.service_state = SimpleNamespace(value="running")
        self.organization_lock = threading.Lock()
        self.organization_service = SimpleNamespace(operation_lock=self.organization_lock, wait=self.organization_wait)
        self.execution_service = SimpleNamespace(wait=self.execution_wait)
        self.code_steward_service = SimpleNamespace(state=self.service_state, stop=self.stop_service,
                                                   wait_stopped=self.wait_service,
                                                   _thread=SimpleNamespace(is_alive=lambda: self.service_thread_alive))
        self.analysis_service = SimpleNamespace(stop=self.stop_ai, current="example.py")
        self.file_event_service = SimpleNamespace(status=self.watch_status,
                                                  observer=SimpleNamespace(is_alive=lambda: self.observer_alive,
                                                                           emitters=[]))
        self.scan_service = SimpleNamespace(get_status=lambda: {"is_scanning": self.scan_running})

    def execution_wait(self, timeout):
        self.calls.append("execution")
        return True

    def organization_wait(self, timeout):
        self.calls.append("organization")
        return True

    def stop_service(self):
        self.calls.append("service")
        self.service_stop.set()
        self.service_state.value = "stopping"

    def wait_service(self, timeout):
        if not self.service_gate.wait(timeout):
            return False
        self.service_state.value = "stopped"
        self.service_thread_alive = False
        self.observer_alive = False
        self.watch_running = False
        return True

    def watch_status(self):
        if self.watcher_error:
            raise RuntimeError("watcher probe failed")
        return {"running": self.watch_running}

    def stop_ai(self, timeout):
        self.ai_stop.set()
        return self.ai_gate.wait(timeout)


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.exited = threading.Event()
        self.manager = ShutdownCoordinator(self.runtime, self.exited.set, poll_seconds=0.01, grace_seconds=60)
        self.app = FastAPI()
        self.app.state.shutdown_coordinator = self.manager
        self.app.include_router(controller.router)
        self.app.add_middleware(ShutdownAdmissionMiddleware, coordinator=self.manager)

        @self.app.post("/write")
        def write():
            return {"ok": True}

        self.addCleanup(self.cleanup_manager)

    def cleanup_manager(self):
        self.runtime.service_gate.set()
        self.runtime.ai_gate.set()
        if self.runtime.organization_lock.locked() and not self.manager._barrier_held:
            self.runtime.organization_lock.release()
        if self.manager._worker:
            self.manager._worker.join(2)
        if self.manager._timer:
            self.manager._timer.cancel()
        if self.manager._barrier_held:
            self.runtime.organization_lock.release()
            self.manager._barrier_held = False

    def wait_state(self, state):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            snapshot = self.manager.snapshot()
            if snapshot["state"] == state:
                return snapshot
            threading.Event().wait(0.005)
        self.fail(f"Expected {state}, got {self.manager.snapshot()}")

    def post(self, path, *, token=None, headers=None, client="127.0.0.1"):
        return asyncio.run(request(self.app, path, method="POST", payload={"token": token or self.manager.token},
                                   headers={"x-code-steward-action": "shutdown", **(headers or {})}, client=client))

    def test_get_is_an_observer_and_token_is_per_launch(self):
        status, headers, body = asyncio.run(request(self.app, "/shutdown/status"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "idle")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertFalse(self.runtime.service_stop.is_set())
        self.assertIsNone(self.manager._worker)
        self.assertNotEqual(self.manager.token, ShutdownCoordinator(self.runtime, lambda: None).token)

    def test_unsupported_get_does_not_stop_anything(self):
        del self.app.state.shutdown_coordinator
        status, _, body = asyncio.run(request(self.app, "/shutdown/status"))
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["supported"])
        self.assertEqual(self.post("/shutdown/start")[0], 503)
        self.assertFalse(self.runtime.service_stop.is_set())

    def test_foreign_clients_hosts_origins_and_missing_intent_are_rejected(self):
        for headers, client in (({}, "192.168.1.2"), ({"host": "evil.example:8000"}, "127.0.0.1"),
                                ({"origin": "https://evil.example"}, "127.0.0.1"),
                                ({"origin": "null"}, "127.0.0.1"),
                                ({"sec-fetch-site": "cross-site"}, "127.0.0.1"),
                                ({"x-code-steward-action": ""}, "127.0.0.1")):
            with self.subTest(headers=headers, client=client):
                self.assertEqual(self.post("/shutdown/start", headers=headers, client=client)[0], 403)
        self.assertFalse(self.runtime.service_stop.is_set())

    def test_wrong_nonce_and_get_control_method_do_not_start_shutdown(self):
        self.assertEqual(self.post("/shutdown/start", token="outdated")[0], 403)
        self.assertEqual(self.post("/shutdown/start", token="失效凭证")[0], 403)
        self.assertEqual(asyncio.run(request(self.app, "/shutdown/start"))[0], 405)
        self.assertEqual(self.manager.snapshot()["state"], "idle")

    def test_same_origin_loopback_post_works(self):
        result = self.post("/shutdown/start", headers={"origin": "http://127.0.0.1:8000", "sec-fetch-site": "same-origin"})
        self.assertEqual(result[0], 202)
        self.wait_state("ready")

    def test_start_is_idempotent_and_finish_before_ready_refused(self):
        self.runtime.service_gate.clear()
        self.assertEqual(self.post("/shutdown/start")[0], 202)
        worker = self.manager._worker
        self.assertEqual(self.post("/shutdown/start")[0], 202)
        self.assertIs(self.manager._worker, worker)
        self.assertEqual(self.post("/shutdown/finish")[0], 409)
        self.assertFalse(self.exited.is_set())

    def test_existing_mutations_drain_before_service_stop_and_new_writes_rejected(self):
        self.assertTrue(self.manager.admit_mutation())
        self.manager.start()
        self.assertFalse(self.runtime.service_stop.wait(0.04))
        self.assertEqual(asyncio.run(request(self.app, "/write", method="POST"))[0], 503)
        self.assertEqual(asyncio.run(request(self.app, "/shutdown/status"))[0], 200)
        self.manager.mutation_finished()
        self.wait_state("ready")
        self.assertEqual(self.runtime.calls[:3], ["execution", "organization", "service"])

    def test_operation_barrier_drains_sync_and_background_work_before_stop(self):
        self.runtime.organization_lock.acquire()
        self.manager.start()
        self.assertFalse(self.runtime.service_stop.wait(0.04))
        self.runtime.organization_lock.release()
        self.wait_state("ready")
        self.assertTrue(self.runtime.organization_lock.locked())
        self.assertFalse(self.exited.is_set())

    def test_ai_completion_is_required_even_after_service_reports_stopped(self):
        self.runtime.ai_gate.clear()
        self.manager.start()
        self.assertTrue(self.runtime.ai_stop.wait(1))
        snapshot = self.manager.snapshot()
        self.assertEqual(self.runtime.service_state.value, "stopped")
        self.assertEqual(snapshot["state"], "closing")
        checks = {item["id"]: item for item in snapshot["checks"]}
        self.assertEqual(checks["ai"]["state"], "pending")
        self.assertFalse(snapshot["can_finish"])
        self.assertFalse(self.exited.is_set())
        self.runtime.ai_gate.set()
        self.wait_state("ready")

    def test_long_wait_advisory_keeps_real_pending_state(self):
        self.runtime.ai_gate.clear()
        self.manager.start()
        self.assertTrue(self.runtime.ai_stop.wait(1))
        self.manager._began = time.monotonic() - 31
        check = next(item for item in self.manager.snapshot()["checks"] if item["id"] == "ai")
        self.assertEqual(check["state"], "pending")
        self.assertIn("30", check["action"])

    def test_error_preserves_gate_and_can_retry_when_worker_has_ended(self):
        self.runtime.watcher_error = True
        self.manager.start()
        self.wait_state("error")
        self.manager._worker.join(1)
        self.assertIn("watcher probe failed", self.manager.snapshot()["error"])
        self.assertFalse(self.manager.admit_mutation())
        self.assertFalse(self.exited.is_set())
        self.runtime.watcher_error = False
        self.manager.start()
        self.wait_state("ready")

    def test_retry_is_refused_until_old_worker_has_returned(self):
        self.manager._state = "error"
        self.manager._worker = SimpleNamespace(is_alive=lambda: True)
        with self.assertRaisesRegex(RuntimeError, "still finishing"):
            self.manager.start()
        self.manager._worker = None

    def test_response_body_is_sent_before_exit_callback(self):
        self.manager.start()
        self.wait_state("ready")
        messages = []
        seen = []

        def exit_callback():
            seen.append(any(item["type"] == "http.response.body" and b'"accepted":true' in item.get("body", b"")
                            for item in messages))

        self.manager.request_exit = exit_callback
        status, _, body = asyncio.run(request(self.app, "/shutdown/finish", method="POST",
                                             payload={"token": self.manager.token},
                                             headers={"x-code-steward-action": "shutdown"}, messages=messages))
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body), {"accepted": True})
        self.assertEqual(seen, [True])
        self.manager.commit()
        self.assertEqual(seen, [True])
        self.assertFalse(self.runtime.organization_lock.locked())

    def test_auto_exit_fallback_runs_only_after_cleanup(self):
        self.manager.grace_seconds = 0.02
        self.runtime.ai_gate.clear()
        self.manager.start()
        self.assertTrue(self.runtime.ai_stop.wait(1))
        self.assertFalse(self.exited.wait(0.05))
        self.runtime.ai_gate.set()
        self.assertTrue(self.exited.wait(1))
        self.assertEqual(self.manager.snapshot()["state"], "exiting")

    def test_callback_failure_is_visible_and_retry_keeps_barrier(self):
        self.manager.start()
        self.wait_state("ready")
        self.manager._worker.join(1)
        self.manager.request_exit = lambda: (_ for _ in ()).throw(RuntimeError("exit rejected"))
        self.manager.commit()
        self.assertEqual(self.manager.snapshot()["state"], "error")
        self.assertIn("exit rejected", self.manager.snapshot()["error"])
        self.assertTrue(self.runtime.organization_lock.locked())
        self.assertFalse(self.manager.admit_mutation())
        self.manager.request_exit = self.exited.set
        self.manager.start()
        self.wait_state("ready")
        self.manager.commit()
        self.assertTrue(self.exited.is_set())

    def test_explicit_assets_and_no_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            expected = {"index.html": b"<!doctype html><title>Bye</title>", "shutdown.css": b"body{}",
                        "shutdown.js": b"console.log('bye')", "farewell.png": b"test-png",
                        "clockoff-turnstile.webp": b"test-webp", "clockoff-turnstile.gif": b"test-gif",
                        "clockoff-poster.png": b"test-poster",
                        "clockoff-badge-turnstile.webp": b"test-badge-webp",
                        "clockoff-badge-turnstile.gif": b"test-badge-gif",
                        "clockoff-badge-poster.png": b"test-badge-poster"}
            for filename, body in expected.items():
                (folder / filename).write_bytes(body)
            with patch.object(controller, "ASSET_ROOT", folder):
                for path, name in (("/shutdown", "index.html"), ("/shutdown/", "index.html"),
                                   ("/shutdown/assets/shutdown.css", "shutdown.css"),
                                   ("/shutdown/assets/shutdown.js", "shutdown.js"),
                                   ("/shutdown/assets/farewell.png", "farewell.png"),
                                   ("/shutdown/assets/clockoff-turnstile.webp", "clockoff-turnstile.webp"),
                                   ("/shutdown/assets/clockoff-turnstile.gif", "clockoff-turnstile.gif"),
                                   ("/shutdown/assets/clockoff-poster.png", "clockoff-poster.png"),
                                   ("/shutdown/assets/clockoff-badge-turnstile.webp", "clockoff-badge-turnstile.webp"),
                                   ("/shutdown/assets/clockoff-badge-turnstile.gif", "clockoff-badge-turnstile.gif"),
                                   ("/shutdown/assets/clockoff-badge-poster.png", "clockoff-badge-poster.png")):
                    status, headers, body = asyncio.run(request(self.app, path))
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["cache-control"], "no-store")
                    self.assertEqual(body, expected[name])
                    media_types = {"clockoff-turnstile.webp": "image/webp", "clockoff-turnstile.gif": "image/gif", "clockoff-poster.png": "image/png",
                                   "clockoff-badge-turnstile.webp": "image/webp", "clockoff-badge-turnstile.gif": "image/gif", "clockoff-badge-poster.png": "image/png"}
                    if name in media_types:
                        self.assertEqual(headers["content-type"], media_types[name])
                self.assertEqual(asyncio.run(request(self.app, "/shutdown/assets/../config.yaml"))[0], 404)

    def test_launcher_injects_owned_server_and_never_loads_global_runtime(self):
        app = FastAPI()
        server = create_server(app, self.runtime, port=8123,
                               server_factory=lambda config: SimpleNamespace(config=config, should_exit=False))
        self.assertEqual(server.config.host, "127.0.0.1")
        self.assertEqual(server.config.port, 8123)
        self.assertFalse(server.config.proxy_headers)
        self.assertFalse(server.should_exit)
        self.assertFalse(self.runtime.service_stop.is_set())
        manager = app.state.shutdown_coordinator
        manager._state = "ready"
        manager.commit()
        self.assertTrue(server.should_exit)


if __name__ == "__main__":
    unittest.main()
