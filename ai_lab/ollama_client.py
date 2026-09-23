"""Ollama HTTP adapter; no SDK, cloud access, tools or shell execution."""
import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .core import LabError, SCHEMA, messages_for


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LabError("Ollama redirected the request; redirects are disabled.", "connection_error")


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
            raise LabError(f"Invalid base_url: {error}", "config_error") from error
        host = "[::1]" if url.hostname == "::1" else "127.0.0.1"
        self.base_url = f"http://{host}:{port}"
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, data=None, timeout=None):
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = Request(self.base_url + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=timeout or self.config["timeout_seconds"]) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise LabError("Ollama response exceeds 8 MiB.", "response_error")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise LabError("Ollama returned a non-object response.", "response_error")
            if value.get("error"):
                raise LabError(str(value["error"]), "model_error")
            return value
        except HTTPError as error:
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise LabError(f"Ollama HTTP {error.code}: {detail}", "model_error") from error
        except (TimeoutError, socket.timeout) as error:
            raise LabError("Ollama timed out. The run stops; check the server before retrying.", "timeout") from error
        except URLError as error:
            code = "timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "connection_error"
            raise LabError("Cannot reach Ollama. Open the Ollama app (or run ollama serve), then retry.", code) from error
        except (ValueError, UnicodeError, OSError) as error:
            raise LabError(f"Invalid/unreadable Ollama response: {error}", "response_error") from error

    def version(self):
        return self.request("/api/version", timeout=5).get("version", "unknown")

    def installed(self):
        models = self.request("/api/tags", timeout=5).get("models")
        if not isinstance(models, list):
            raise LabError("Ollama /api/tags did not return a model list.", "response_error")
        return models

    def running(self):
        models = self.request("/api/ps", timeout=5).get("models")
        if not isinstance(models, list):
            raise LabError("Ollama /api/ps did not return a model list.", "response_error")
        return models

    @staticmethod
    def match(models, tag):
        canonical = tag if ":" in tag.rsplit("/", 1)[-1] else tag + ":latest"
        return next((m for m in models if m.get("name", m.get("model")) in {tag, canonical}), None)

    def pull(self, tag):
        print(f"Downloading {tag}. This can take several minutes; progress is shown below.", flush=True)
        req = Request(self.base_url + "/api/pull", data=json.dumps({"model": tag, "stream": True}).encode(),
                      headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=300) as response:
                previous = None
                success = False
                while True:
                    line = response.readline(1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 1024 * 1024:
                        raise LabError("Oversized download progress response.", "download_error")
                    event = json.loads(line)
                    if event.get("error"):
                        raise LabError(str(event["error"]), "download_error")
                    status = event.get("status", "downloading")
                    total = event.get("total", 0)
                    bucket = min(100, int(event.get("completed", 0) * 100 / total) // 10 * 10) if total else None
                    key = (status, event.get("digest"), bucket)
                    if key != previous:
                        print(f"  {status}" + (f" {bucket}%" if bucket is not None else ""), flush=True)
                        previous = key
                    success = status == "success"
                if not success:
                    raise LabError("Download ended before success. Retry the same command to resume.", "download_error")
        except (HTTPError, URLError, OSError, ValueError) as error:
            raise LabError(f"Model download failed: {error}. Retry to resume.", "download_error") from error

    def prepare(self, tag, allow_pull=False):
        installed = self.match(self.installed(), tag)
        if installed is None and allow_pull:
            self.pull(tag)
            installed = self.match(self.installed(), tag)
        if installed is None:
            raise LabError(f"Model is not installed: {tag}. Add --pull, or run: ollama pull {tag}", "missing_model")
        info = self.request("/api/show", {"model": tag}, timeout=15)
        if info.get("remote_host") or info.get("remote_model"):
            raise LabError("Cloud-backed models are not supported in the local test lab.", "cloud_model")
        running = self.running()
        other = [m.get("name", m.get("model", "unknown")) for m in running if self.match([m], tag) is None]
        if other:
            raise LabError("Other models are loaded: " + ", ".join(other) + ". Close other AI clients / stop those models first.", "busy")
        # Establish the same starting condition for each candidate.
        self.unload(tag)
        return {"name": tag, "digest": installed.get("digest"), "details": info.get("details", {}),
                "download_bytes": installed.get("size")}

    def unload(self, tag):
        self.request("/api/generate", {"model": tag, "keep_alive": 0, "stream": False}, timeout=30)
        if self.match(self.running(), tag) is not None:
            raise LabError("Model is still loaded after unload. Close other clients and retry.", "busy")

    def analyze(self, tag, case):
        options = {k: self.config[k] for k in ("num_ctx", "num_predict", "num_thread", "temperature", "seed")}
        options["num_gpu"] = 0 if self.config["device"] == "cpu" else -1
        return self.request("/api/chat", {
            "model": tag, "messages": messages_for(case), "format": SCHEMA,
            "stream": False, "think": False, "keep_alive": "2m",
            "truncate": False, "shift": False, "options": options,
        })
