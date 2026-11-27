"""Transport checks use disposable loopback fixtures, never a real Ollama model."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai.common import AIError
from ai.explain.ollama_client import OllamaClient
from ai.explain.pipeline import repair_once
from Services.GovernedOllamaClient import GovernedOllamaClient
from Services.ResourceGovernor import ResourceCancelled


class FakeGovernor:
    def __init__(self, cap=2):
        self.busy = threading.Event()
        self.closing = threading.Event()
        self.slot = threading.Lock()
        self.cap = cap
        self.cancelled = 0

    def should_cancel_ai(self):
        return self.busy.is_set() or self.closing.is_set()

    def checkpoint(self, cancel=None):
        while self.busy.is_set():
            if self.closing.wait(0.01):
                raise ResourceCancelled("closing")
        if self.closing.is_set():
            raise ResourceCancelled("closing")

    @contextmanager
    def ai_slot(self):
        with self.slot:
            yield

    def ai_thread_limit(self, requested="auto"):
        return min(requested, self.cap) if type(requested) is int else self.cap

    def note_ai_cancelled(self):
        self.cancelled += 1


def lines(*chunks):
    return b"".join(json.dumps(chunk).encode() + b"\n" for chunk in chunks)


_CHAT = lines({"message": {"content": "hello ", "thinking": "a"}, "done": False},
              {"message": {"content": "world", "thinking": "b"}, "done": True,
               "done_reason": "stop", "eval_count": 2})


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = False


@contextmanager
def server(action=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.server.requests.append((self.path, json.loads(body)))
            try:
                (self.server.action or normal)(_self=self)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    fixture = FixtureServer(("127.0.0.1", 0), Handler)
    fixture.action = action
    fixture.requests = []
    fixture.entered = threading.Event()
    fixture.disconnected = threading.Event()
    fixture.release = threading.Event()
    thread = threading.Thread(target=fixture.serve_forever, kwargs={"poll_interval": 0.01}, name="fixture-http")
    thread.start()
    try:
        yield fixture
    finally:
        fixture.release.set()
        fixture.shutdown()
        fixture.server_close()
        thread.join(3)
        if thread.is_alive():
            raise AssertionError("Fixture server thread did not stop")


def normal(_self, payload=_CHAT, status=200):
    _self.send_response(status)
    _self.send_header("Content-Type", "application/x-ndjson")
    _self.send_header("Content-Length", str(len(payload)))
    _self.end_headers()
    _self.wfile.write(payload)
    _self.wfile.flush()


def delayed_headers(_self):
    _self.server.entered.set()
    _self.connection.settimeout(2)
    if _self.connection.recv(1) == b"":
        _self.server.disconnected.set()


def delayed_stream(_self):
    _self.send_response(200)
    _self.send_header("Content-Type", "application/x-ndjson")
    _self.send_header("Connection", "close")
    _self.end_headers()
    _self.wfile.write(lines({"message": {"content": "partial"}, "done": False}))
    _self.wfile.flush()
    _self.server.entered.set()
    _self.connection.settimeout(2)
    if _self.connection.recv(1) == b"":
        _self.server.disconnected.set()


class GovernedOllamaTests(unittest.TestCase):
    def setUp(self):
        self.guards_before = {thread.ident for thread in threading.enumerate()
                              if thread.name == "steward-ai-request-guard"}

    def tearDown(self):
        guards_after = {thread.ident for thread in threading.enumerate()
                        if thread.name == "steward-ai-request-guard"}
        self.assertEqual(guards_after, self.guards_before, "A request guard thread leaked")

    def client(self, fixture, governor=None, **changes):
        config = {"base_url": f"http://127.0.0.1:{fixture.server_port}", "timeout_seconds": 2,
                  "num_ctx": 1024, "num_predict": 256, "num_thread": "auto", "device": "cpu",
                  "temperature": 0, "seed": 1, **changes}
        return GovernedOllamaClient(config, governor or FakeGovernor(), watch_interval=0.01)

    def test_merges_chat_and_preserves_metrics_callback_and_payload(self):
        with server() as fixture:
            client = self.client(fixture)
            payload = {"model": "test", "stream": False, "options": {"num_thread": 8, "num_gpu": 0}}
            chunks = []
            result = client.request("/api/chat", payload, on_chunk=chunks.append)
            self.assertEqual(result["message"], {"content": "hello world", "thinking": "ab"})
            self.assertEqual(result["eval_count"], 2)
            self.assertEqual(len(chunks), 2)
            sent = fixture.requests[0][1]
            self.assertTrue(sent["stream"])
            self.assertEqual(sent["options"], {"num_thread": 2, "num_gpu": 0})
            self.assertEqual(payload["options"]["num_thread"], 8)
            self.assertFalse(payload["stream"])

    def test_explicit_one_thread_is_not_increased(self):
        with server() as fixture:
            self.client(fixture).request("/api/chat", {"options": {"num_thread": 1}})
            self.assertEqual(fixture.requests[0][1]["options"]["num_thread"], 1)

    def test_ask_analyze_and_repair_use_same_governed_transport(self):
        with server() as fixture:
            client = self.client(fixture)
            with patch("ai.explain.ollama_client.messages_for", return_value=[]), \
                    patch("ai.explain.ollama_client.source_view", return_value=("1 hi", [1], 0)):
                self.assertEqual(client.ask("test", [], {})["message"]["content"], "hello world")
                self.assertEqual(client.analyze("test", {})["message"]["content"], "hello world")
                self.assertEqual(client.repair("test", {}, "old", "fix")["message"]["content"], "hello world")
            self.assertEqual(len(fixture.requests), 3)
            self.assertTrue(all(item[1]["stream"] for item in fixture.requests))
            self.assertTrue(all(item[1]["options"]["num_thread"] == 2 for item in fixture.requests))
            self.assertEqual(client.config["num_thread"], "auto")

    def test_metadata_requests_delegate_unchanged(self):
        client = GovernedOllamaClient({"base_url": "http://127.0.0.1:1", "timeout_seconds": 2}, FakeGovernor())
        with patch.object(OllamaClient, "request", return_value={"unchanged": True}) as base:
            self.assertEqual(client.request("/api/tags", timeout=5), {"unchanged": True})
            base.assert_called_once_with("/api/tags", None, 5, None)

    def test_generate_merges_response_instead_of_chat_message(self):
        payload = lines({"response": "good ", "done": False},
                        {"response": "bye", "done": True, "eval_count": 2})
        with server(lambda _self: normal(_self, payload)) as fixture:
            result = self.client(fixture).request("/api/generate", {"model": "test"})
            self.assertEqual(result["response"], "good bye")
            self.assertEqual(result["eval_count"], 2)

    def cancellation_case(self, action):
        governor = FakeGovernor()
        with server(action) as fixture:
            client = self.client(fixture, governor)
            outcome = []

            def run():
                try:
                    client.request("/api/chat", {"model": "test"})
                except AIError as error:
                    outcome.append(error.code)

            thread = threading.Thread(target=run, name="fixture-client")
            thread.start()
            self.assertTrue(fixture.entered.wait(1), "Fixture request never entered")
            started = time.monotonic()
            governor.busy.set()
            thread.join(1)
            self.assertFalse(thread.is_alive(), "Cancellation did not interrupt blocked HTTP read")
            self.assertEqual(outcome, ["resource_busy"])
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(fixture.disconnected.wait(1), "Our connection was not closed")
            self.assertEqual(governor.cancelled, 1)

    def test_pressure_cancels_while_headers_and_prompt_processing_are_pending(self):
        self.cancellation_case(delayed_headers)

    def test_pressure_cancels_during_http10_stream_with_socket_file(self):
        self.cancellation_case(delayed_stream)

    def test_busy_before_start_defers_without_opening_connection(self):
        governor = FakeGovernor()
        governor.busy.set()
        with server() as fixture:
            client = self.client(fixture, governor)
            result = []
            thread = threading.Thread(target=lambda: result.append(client.request("/api/chat", {})))
            thread.start()
            time.sleep(0.04)
            self.assertEqual(fixture.requests, [])
            governor.busy.clear()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0]["message"]["content"], "hello world")
            self.assertEqual(governor.cancelled, 0)

    def test_closing_interrupts_waiting_admission(self):
        governor = FakeGovernor()
        governor.busy.set()
        governor.closing.set()
        with server() as fixture:
            with self.assertRaises(AIError) as caught:
                self.client(fixture, governor).request("/api/chat", {})
            self.assertEqual(caught.exception.code, "resource_busy")
            self.assertEqual(fixture.requests, [])
            self.assertEqual(governor.cancelled, 0)

    def test_pressure_racing_first_send_cannot_reconnect_after_guard_closed_socket(self):
        governor = FakeGovernor()
        original_request = http.client.HTTPConnection.request

        def send_after_cancellation(connection, *args, **kwargs):
            governor.busy.set()
            deadline = time.monotonic() + 0.5
            while connection.sock is not None and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIsNone(connection.sock)
            return original_request(connection, *args, **kwargs)

        with server() as fixture:
            with patch.object(http.client.HTTPConnection, "request", send_after_cancellation):
                with self.assertRaises(AIError) as caught:
                    self.client(fixture, governor).request("/api/chat", {})
            self.assertEqual(caught.exception.code, "resource_busy")
            self.assertEqual(fixture.requests, [], "Cancelled HTTPConnection reconnected and sent a new request")
            self.assertEqual(governor.cancelled, 1)

    def test_monitor_fault_defers_without_starting_generation(self):
        governor = FakeGovernor()
        governor.should_cancel_ai = lambda: (_ for _ in ()).throw(RuntimeError("sampling failed"))
        with server() as fixture:
            with self.assertRaises(AIError) as caught:
                self.client(fixture, governor).request("/api/chat", {})
            self.assertEqual(caught.exception.code, "resource_busy")
            self.assertEqual(fixture.requests, [])
            self.assertEqual(governor.cancelled, 0)

    def test_generation_calls_share_one_serial_slot(self):
        governor = FakeGovernor()

        def hold_first(_self):
            if len(_self.server.requests) == 1:
                _self.server.entered.set()
                _self.server.release.wait(1)
            normal(_self)

        with server(hold_first) as fixture:
            client = self.client(fixture, governor)
            results = []
            threads = [threading.Thread(target=lambda: results.append(client.request("/api/chat", {}))) for _ in range(2)]
            threads[0].start()
            self.assertTrue(fixture.entered.wait(1))
            threads[1].start()
            time.sleep(0.04)
            self.assertEqual(len(fixture.requests), 1, "Two generations were admitted concurrently")
            fixture.release.set()
            for thread in threads:
                thread.join(1)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 2)

    def test_cancelling_our_request_does_not_stop_shared_server_or_other_clients(self):
        governor = FakeGovernor()

        def shared(_self):
            if len(_self.server.requests) == 1:
                delayed_headers(_self)
            else:
                normal(_self, json.dumps({"peer": "still running"}).encode())

        with server(shared) as fixture:
            client = self.client(fixture, governor)
            errors = []

            def run():
                try:
                    client.request("/api/chat", {})
                except AIError as error:
                    errors.append(error.code)

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(fixture.entered.wait(1))
            governor.busy.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            peer = OllamaClient(client.config)
            self.assertEqual(peer.request("/api/chat", {}), {"peer": "still running"})
            self.assertEqual(errors, ["resource_busy"])

    def test_wall_deadline_interrupts_headers_without_waiting_socket_timeout(self):
        with server(delayed_headers) as fixture:
            started = time.monotonic()
            with self.assertRaises(AIError) as caught:
                self.client(fixture).request("/api/chat", {}, timeout=0.12)
            self.assertEqual(caught.exception.code, "timeout")
            self.assertLess(time.monotonic() - started, 0.6)
            self.assertTrue(fixture.disconnected.wait(1))

    def test_wall_deadline_cannot_be_extended_by_slow_dribble(self):
        def dribble(_self):
            _self.send_response(200)
            _self.send_header("Connection", "close")
            _self.end_headers()
            for _ in range(50):
                _self.wfile.write(b" ")
                _self.wfile.flush()
                time.sleep(0.02)

        with server(dribble) as fixture:
            started = time.monotonic()
            with self.assertRaises(AIError) as caught:
                self.client(fixture).request("/api/chat", {}, timeout=0.12)
            self.assertEqual(caught.exception.code, "timeout")
            self.assertLess(time.monotonic() - started, 0.6)

    def test_http_error_redirect_bad_json_and_truncated_stream_keep_error_codes(self):
        cases = [(500, b"unavailable", "model_error"), (302, b"", "connection_error"),
                 (200, b"not json\n", "response_error"),
                 (200, lines({"error": "bad model"}), "model_error"),
                 (200, lines({"message": {"content": "partial"}, "done": False}), "response_error")]
        for status, payload, code in cases:
            with self.subTest(status=status, payload=payload), server(lambda _self: normal(_self, payload, status)) as fixture:
                with self.assertRaises(AIError) as caught:
                    self.client(fixture).request("/api/chat", {})
                self.assertEqual(caught.exception.code, code)

    def test_response_over_eight_mib_is_rejected(self):
        payload = b" " * (8 * 1024 * 1024 + 1) + b"\n"
        with server(lambda _self: normal(_self, payload)) as fixture:
            with self.assertRaises(AIError) as caught:
                self.client(fixture).request("/api/chat", {})
            self.assertEqual(caught.exception.code, "response_error")
            self.assertIn("8 MiB", str(caught.exception))

    def test_repair_resource_pressure_is_not_swallowed_as_repair_rejection(self):
        def repair(*_):
            raise AIError("yield", "resource_busy")

        with patch("ai.explain.pipeline.repair_message", return_value="fix"):
            with self.assertRaises(AIError) as caught:
                repair_once(SimpleNamespace(repair=repair), "test", {}, "old", {}, [])
            self.assertEqual(caught.exception.code, "resource_busy")


if __name__ == "__main__":
    unittest.main()
