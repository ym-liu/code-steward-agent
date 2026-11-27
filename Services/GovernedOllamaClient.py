"""Request-scoped CPU admission and cancellation for the existing local AI adapter.

Only our generation sockets are controlled. The shared Ollama service, models,
and other clients' requests are never stopped, suspended, or reprioritized.
"""
import http.client
import json
import socket
import threading
import time
from urllib.parse import urlsplit

from ai.common import AIError
from ai.explain.ollama_client import OllamaClient
from Services.ResourceGovernor import ResourceCancelled


_LIMIT = 8 * 1024 * 1024
_GENERATION_PATHS = {"/api/chat", "/api/generate"}


class _RequestGuard:
    """Watch even while HTTP headers or the first inference token are pending."""

    def __init__(self, governor, connection, timeout, interval):
        self.governor = governor
        self.connection = connection
        self.deadline = time.monotonic() + timeout
        self.interval = interval
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.reason = None
        self.sock = None
        self.generation_started = False
        self.thread = threading.Thread(target=self._watch, name="steward-ai-request-guard", daemon=True)

    def start(self):
        self.thread.start()

    def attach(self, sock):
        with self.lock:
            self.sock = sock
            reason = self.reason
        if reason is not None:
            self._interrupt()
            self.raise_if_cancelled()

    def _interrupt(self):
        with self.lock:
            sock = self.sock
        if sock is not None:
            try:
                # shutdown interrupts blocked socket-file reads, including reads
                # which still own an HTTP/1.0 response after connection.close().
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.connection.close()

    def cancel(self, reason):
        with self.lock:
            if self.reason is not None:
                return
            self.reason = reason
            count_cancel = reason == "resource_busy" and self.generation_started
        self._interrupt()
        if count_cancel:
            note = getattr(self.governor, "note_ai_cancelled", None)
            if note is not None:
                try:
                    note()
                except Exception:
                    pass  # telemetry cannot prevent a resource-pressure cancellation

    def begin_generation(self):
        self.raise_if_cancelled()
        with self.lock:
            if self.reason is None:
                self.generation_started = True
        self.raise_if_cancelled()

    def _watch(self):
        while not self.done.is_set():
            self.check()
            if self.reason is not None or self.done.wait(self.interval):
                return

    def check(self):
        try:
            busy = self.governor.should_cancel_ai()
        except Exception:
            busy = True  # monitoring failure cannot authorize heavy work
        if busy:
            self.cancel("resource_busy")
        elif time.monotonic() >= self.deadline:
            self.cancel("timeout")

    def raise_if_cancelled(self):
        self.check()
        if self.reason == "resource_busy":
            raise AIError("AI work was deferred to yield CPU to other applications.", "resource_busy")
        if self.reason == "timeout":
            raise AIError("Ollama timed out. The run stops; check the server before retrying.", "timeout")

    def stop(self):
        self.done.set()
        self.thread.join()


class GovernedOllamaClient(OllamaClient):
    """Preserve prompts and validation while governing only generation requests."""

    def __init__(self, config, governor, *, watch_interval=0.1):
        super().__init__(config)
        self.governor = governor
        self.watch_interval = watch_interval

    def request(self, path, data=None, timeout=None, on_chunk=None):
        if data is None or path not in _GENERATION_PATHS:
            return super().request(path, data, timeout, on_chunk)
        try:
            with self.governor.ai_slot():
                self.governor.checkpoint()
                return self._generate(path, data, timeout, on_chunk)
        except ResourceCancelled as error:
            raise AIError(f"AI work was deferred: {error}", "resource_busy") from error

    def _generate(self, path, data, timeout, on_chunk):
        if not isinstance(data, dict):
            raise AIError("Ollama generation payload must be an object.", "config_error")
        payload = dict(data)
        options = dict(payload.get("options") or {})
        # Keep this cap stable across load changes: num_thread is a runner-load
        # option in Ollama, so changing it every sample can reload a shared model.
        options["num_thread"] = self.governor.ai_thread_limit(options.get("num_thread", "auto"))
        payload.update(stream=True, options=options)
        try:
            body = json.dumps(payload).encode("utf-8")
            seconds = float(timeout if timeout is not None else self.config["timeout_seconds"])
            if not 0 < seconds < float("inf"):
                raise ValueError("timeout must be finite and positive")
        except (TypeError, ValueError, OverflowError) as error:
            raise AIError(f"Invalid Ollama request: {error}", "config_error") from error

        address = urlsplit(self.base_url)  # already restricted to loopback by the base adapter
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=seconds)
        # A cancellation can race the next send. Never let HTTPConnection
        # silently reconnect after the guard closed its only owned socket.
        connection.auto_open = False
        guard = _RequestGuard(self.governor, connection, seconds, self.watch_interval)
        response = None
        connected = False
        guard.start()
        try:
            guard.raise_if_cancelled()
            # Expose our socket before connect, so the guard owns it throughout
            # connect, response headers, prompt evaluation, and streamed output.
            family = socket.AF_INET6 if ":" in address.hostname else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(seconds)
            connection.sock = sock
            guard.attach(sock)
            guard.raise_if_cancelled()
            sock.connect((address.hostname, address.port))
            connected = True
            guard.raise_if_cancelled()
            guard.begin_generation()
            connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            guard.raise_if_cancelled()
            if 300 <= response.status < 400:
                raise AIError("Ollama redirected the request; redirects are disabled.", "connection_error")
            if response.status >= 400:
                detail = response.read(4096).decode("utf-8", errors="replace")
                raise AIError(f"Ollama HTTP {response.status}: {detail}", "model_error")

            def report(chunk):
                guard.raise_if_cancelled()
                if on_chunk is not None:
                    on_chunk(chunk)

            if path == "/api/chat":
                value = self.read_chat_stream(response, report)
            else:
                value = self._read_generate_stream(response, report)
            guard.raise_if_cancelled()
            return value
        except AIError:
            guard.raise_if_cancelled()
            raise
        except (TimeoutError, socket.timeout) as error:
            guard.raise_if_cancelled()
            raise AIError("Ollama timed out. The run stops; check the server before retrying.", "timeout") from error
        except (OSError, http.client.HTTPException) as error:
            guard.raise_if_cancelled()
            if not connected:
                raise AIError("Cannot reach Ollama. Open the Ollama app (or run ollama serve), then retry.", "connection_error") from error
            raise AIError(f"Invalid/unreadable Ollama response: {error}", "response_error") from error
        except (ValueError, UnicodeError, TypeError) as error:
            guard.raise_if_cancelled()
            raise AIError(f"Invalid/unreadable Ollama response: {error}", "response_error") from error
        finally:
            guard.stop()
            if response is not None:
                response.close()
            connection.close()

    @staticmethod
    def _read_generate_stream(response, on_chunk):
        pieces, thinking = [], []
        size = 0
        while True:
            line = response.readline(_LIMIT + 1)
            if not line:
                raise AIError("Ollama stream ended before completion.", "response_error")
            size += len(line)
            if size > _LIMIT:
                raise AIError("Ollama response exceeds 8 MiB.", "response_error")
            if not line.strip():
                continue
            chunk = json.loads(line)
            if not isinstance(chunk, dict):
                raise AIError("Ollama returned a non-object stream response.", "response_error")
            if chunk.get("error"):
                raise AIError(str(chunk["error"]), "model_error")
            for key, target in (("response", pieces), ("thinking", thinking)):
                value = chunk.get(key, "")
                if not isinstance(value, str):
                    raise AIError("Ollama stream returned non-text output.", "response_error")
                target.append(value)
            on_chunk(chunk)
            if chunk.get("done") is True:
                result = {**chunk, "response": "".join(pieces)}
                if any(thinking):
                    result["thinking"] = "".join(thinking)
                return result
