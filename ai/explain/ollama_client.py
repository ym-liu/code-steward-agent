"""Ollama HTTP adapter; no SDK, cloud access, tools or shell execution."""
import json
from copy import deepcopy
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from ..common import AIError, digest
from .answer import SCHEMA
from .request import messages_for, source_view


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AIError("Ollama redirected the request; redirects are disabled.", "connection_error")


class OllamaClient:
    def __init__(self, config):
        self.config = config
        try:
            url = urlsplit(config["base_url"])
            if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
                raise ValueError("only loopback HTTP addresses are allowed")
            port = url.port or 11434
        except (TypeError, ValueError) as error:
            raise AIError(f"Invalid base_url: {error}", "config_error") from error
        host = "[::1]" if url.hostname == "::1" else "127.0.0.1"
        self.base_url = f"http://{host}:{port}"
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, data=None, timeout=None, on_chunk=None):
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = Request(self.base_url + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout or self.config["timeout_seconds"]) as response:
                if on_chunk is not None:
                    return self.read_chat_stream(response, on_chunk)
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise AIError("Ollama response exceeds 8 MiB.", "response_error")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise AIError("Ollama returned a non-object response.", "response_error")
            if value.get("error"):
                raise AIError(str(value["error"]), "model_error")
            return value
        except HTTPError as error:
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise AIError(f"Ollama HTTP {error.code}: {detail}", "model_error") from error
        except (TimeoutError, socket.timeout) as error:
            raise AIError("Ollama timed out. The run stops; check the server before retrying.", "timeout") from error
        except URLError as error:
            code = "timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "connection_error"
            raise AIError("Cannot reach Ollama. Open the Ollama app (or run ollama serve), then retry.", code) from error
        except (ValueError, UnicodeError, OSError) as error:
            raise AIError(f"Invalid/unreadable Ollama response: {error}", "response_error") from error

    @staticmethod
    def read_chat_stream(response, on_chunk):
        """Join NDJSON message pieces and retain the final authoritative metrics."""
        content, thinking, tools = [], [], []
        size = 0
        while True:
            line = response.readline(8 * 1024 * 1024 + 1)
            if not line:
                raise AIError("Ollama stream ended before completion.", "response_error")
            size += len(line)
            if size > 8 * 1024 * 1024:
                raise AIError("Ollama response exceeds 8 MiB.", "response_error")
            if not line.strip():
                continue
            chunk = json.loads(line)
            if not isinstance(chunk, dict):
                raise AIError("Ollama returned a non-object stream response.", "response_error")
            if chunk.get("error"):
                raise AIError(str(chunk["error"]), "model_error")
            message = chunk.get("message")
            if not isinstance(message, dict):
                raise AIError("Ollama stream returned no text message.", "response_error")
            for key, pieces in (("content", content), ("thinking", thinking)):
                value = message.get(key, "")
                if not isinstance(value, str):
                    raise AIError("Ollama stream returned a non-text message.", "response_error")
                pieces.append(value)
            if message.get("tool_calls"):
                tools.extend(message["tool_calls"])
            on_chunk(chunk)
            if chunk.get("done") is True:
                merged = {**message, "content": "".join(content)}
                if any(thinking):
                    merged["thinking"] = "".join(thinking)
                if tools:
                    merged["tool_calls"] = tools
                return {**chunk, "message": merged}

    def version(self):
        return self.request("/api/version", timeout=5).get("version", "unknown")

    def installed(self):
        models = self.request("/api/tags", timeout=5).get("models")
        if not isinstance(models, list):
            raise AIError("Ollama /api/tags did not return a model list.", "response_error")
        return models

    def running(self):
        models = self.request("/api/ps", timeout=5).get("models")
        if not isinstance(models, list):
            raise AIError("Ollama /api/ps did not return a model list.", "response_error")
        return models

    @staticmethod
    def match(models, tag):
        canonical = tag if ":" in tag.rsplit("/", 1)[-1] else tag + ":latest"
        return next((m for m in models if m.get("name", m.get("model")) in {tag, canonical}), None)

    def analyze(self, tag, case, on_chunk=None):
        return self.chat(tag, case, messages_for(case, self.config), on_chunk)

    def repair(self, tag, case, previous, feedback, on_chunk=None):
        """One follow-up turn: the model sees its own answer and the program's findings."""
        messages = messages_for(case, self.config) + [{"role": "assistant", "content": previous},
                                         {"role": "user", "content": feedback}]
        return self.chat(tag, case, messages, on_chunk)

    def options(self, keys):
        options = {k: self.config[k] for k in keys}
        if options.get("num_thread") == "auto":
            del options["num_thread"]  # Ollama then uses the physical performance cores
        options["num_gpu"] = 0 if self.config["device"] == "cpu" else -1
        return options

    def chat(self, tag, case, messages, on_chunk=None):
        options = self.options(("num_ctx", "num_predict", "num_thread", "temperature", "seed"))
        schema = deepcopy(SCHEMA)
        # Every real nonempty input can cite a visible line, even for an unknown purpose.
        # Let the decoder select a real position instead of inventing a line number.
        schema["properties"]["evidence"]["minItems"] = 1
        schema["properties"]["evidence"]["items"]["properties"]["line"]["enum"] = source_view(case, self.config)[1]
        response = self.request("/api/chat", {
            "model": tag, "messages": messages, "format": schema,
            "stream": on_chunk is not None, "think": False, "keep_alive": "2m",
            "truncate": False, "shift": False, "options": options,
        }, on_chunk=on_chunk)
        response["requested_schema"] = schema
        response["requested_schema_hash"] = digest(json.dumps(schema, sort_keys=True))
        return response

    def ask(self, tag, messages, schema, num_predict=256):
        """A short structured request after the explanations, for example choosing a topic folder."""
        options = self.options(("num_ctx", "num_thread", "temperature", "seed"))
        options["num_predict"] = min(num_predict, self.config["num_predict"])
        return self.request("/api/chat", {
            "model": tag, "messages": messages, "format": schema, "stream": False, "think": False,
            "keep_alive": "2m", "truncate": False, "shift": False, "options": options,
        })
