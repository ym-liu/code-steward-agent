"""Navigation tests use ASGI messages and a fresh FastAPI app; no local services."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

from fastapi import FastAPI

from Services.WorkspaceNavigation import WorkspaceNavigationMiddleware, install_workspace_navigation
from launch import create_server


async def request(app, *, path="/docs", method="GET", kind="http", query=b""):
    messages = []
    scope = {"type": kind, "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": query, "root_path": "", "headers": [(b"host", b"127.0.0.1:8000")],
             "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 1234)}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def body(messages):
    return b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")


class WorkspaceNavigationTests(unittest.TestCase):
    def fake_app(self, *, status=200, content_type=b"text/html; charset=utf-8", chunks=None, headers=None):
        chunks = chunks or [b'<html><body class="docs">Swagger<script>url: "/openapi.json"</script></body></html>']
        events = [{"type": "http.response.start", "status": status,
                   "headers": [(b"content-type", content_type),
                               (b"content-length", str(sum(map(len, chunks))).encode()),
                               (b"x-preserve", b"yes"), *(headers or [])]}]
        events.extend({"type": "http.response.body", "body": chunk, "more_body": index + 1 < len(chunks)}
                      for index, chunk in enumerate(chunks))

        async def app(scope, receive, send):
            for event in events:
                await send(event)

        return app, events

    def test_successful_html_preserves_original_body_and_adds_english_links(self):
        app, original = self.fake_app()
        messages = asyncio.run(request(WorkspaceNavigationMiddleware(app)))
        html = body(messages)
        self.assertIn(b'href="/startup"', html)
        self.assertIn(b'href="/shutdown"', html)
        self.assertIn(b">Shut down</a>", html)
        self.assertIn(b'aria-label="Application controls"', html)
        self.assertIn(b'Swagger<script>url: "/openapi.json"</script>', html)
        self.assertIn(b'<body class="docs">', html)
        headers = dict(messages[0]["headers"])
        self.assertEqual(int(headers[b"content-length"]), len(html))
        self.assertEqual(headers[b"x-preserve"], b"yes")
        self.assertEqual(messages[0]["status"], original[0]["status"])

    def test_split_body_tag_and_streamed_chunks_are_handled(self):
        app, _ = self.fake_app(chunks=[b"<HTML><BO", b'DY class="original">', b"Swagger</BODY></HTML>"])
        messages = asyncio.run(request(WorkspaceNavigationMiddleware(app)))
        self.assertIn(b'<BODY class="original">', body(messages))
        self.assertIn(b'id="code-steward-workspace-nav"', body(messages))
        self.assertFalse(messages[-1].get("more_body", False))
        self.assertEqual(int(dict(messages[0]["headers"])[b"content-length"]), len(body(messages)))

    def test_non_docs_requests_are_unchanged(self):
        app, original = self.fake_app()
        for path in ("/", "/startup", "/shutdown", "/openapi.json", "/docs/", "/docs/oauth2-redirect", "/organization/plan"):
            with self.subTest(path=path):
                self.assertEqual(asyncio.run(request(WorkspaceNavigationMiddleware(app), path=path)), original)

    def test_non_get_methods_are_unchanged(self):
        app, original = self.fake_app()
        for method in ("HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(asyncio.run(request(WorkspaceNavigationMiddleware(app), method=method)), original)

    def test_non_http_scope_is_unchanged(self):
        app, original = self.fake_app()
        self.assertEqual(asyncio.run(request(WorkspaceNavigationMiddleware(app), kind="websocket")), original)

    def test_unsuccessful_responses_are_unchanged(self):
        for status in (201, 204, 301, 304, 404, 500):
            with self.subTest(status=status):
                app, original = self.fake_app(status=status)
                self.assertEqual(asyncio.run(request(WorkspaceNavigationMiddleware(app))), original)

    def test_non_html_responses_are_unchanged(self):
        for content_type in (b"application/json", b"text/plain", b"application/xhtml+xml", b""):
            with self.subTest(content_type=content_type):
                app, original = self.fake_app(content_type=content_type)
                self.assertEqual(asyncio.run(request(WorkspaceNavigationMiddleware(app))), original)

    def test_compressed_responses_are_unchanged(self):
        for encoding in (b"gzip", b"br", b"deflate", b"gzip, br"):
            with self.subTest(encoding=encoding):
                app, original = self.fake_app(headers=[(b"content-encoding", encoding)])
                self.assertEqual(asyncio.run(request(WorkspaceNavigationMiddleware(app))), original)

    def test_header_case_and_html_mime_case_are_supported(self):
        app, events = self.fake_app(content_type=b"Text/HTML; charset=UTF-8")
        events[0]["headers"][0] = (b"Content-Type", b"Text/HTML; charset=UTF-8")
        messages = asyncio.run(request(WorkspaceNavigationMiddleware(app)))
        self.assertIn(b'id="code-steward-workspace-nav"', body(messages))

    def test_missing_body_tag_leaves_original_messages_untouched(self):
        app, original = self.fake_app(chunks=[b"<html><h1>partial", b" HTML</h1></html>"])
        self.assertEqual(asyncio.run(request(WorkspaceNavigationMiddleware(app))), original)

    def test_preexisting_navigation_is_not_added_twice(self):
        app, original = self.fake_app(chunks=[b'<html><body><nav id="code-steward-workspace-nav">Existing</nav></body></html>'])
        self.assertEqual(asyncio.run(request(WorkspaceNavigationMiddleware(app))), original)

    def test_entity_hashes_are_removed_after_html_changes(self):
        app, _ = self.fake_app(headers=[(b"ETag", b"old"), (b"Content-MD5", b"old"),
                                        (b"Digest", b"old"), (b"Content-Digest", b"old")])
        messages = asyncio.run(request(WorkspaceNavigationMiddleware(app)))
        keys = {key.lower() for key, _ in messages[0]["headers"]}
        self.assertTrue(keys.isdisjoint({b"etag", b"content-md5", b"digest", b"content-digest"}))

    def test_navigation_installation_is_idempotent(self):
        app = FastAPI()
        install_workspace_navigation(app)
        install_workspace_navigation(app)
        self.assertEqual(sum(item.cls is WorkspaceNavigationMiddleware for item in app.user_middleware), 1)
        self.assertEqual(body(asyncio.run(request(app))).count(b'id="code-steward-workspace-nav"'), 1)

    def test_real_swagger_and_openapi_are_preserved(self):
        app = FastAPI(openapi_url="/custom/schema.json", swagger_ui_parameters={"deepLinking": False})
        original = body(asyncio.run(request(app)))
        install_app = FastAPI(openapi_url="/custom/schema.json", swagger_ui_parameters={"deepLinking": False})
        install_workspace_navigation(install_app)
        updated = body(asyncio.run(request(install_app)))
        self.assertIn(b'"deepLinking": false', updated)
        self.assertIn(b"url: '/custom/schema.json'", updated)
        self.assertIn(b"swagger-ui-bundle.js", updated)
        self.assertIn(b"swagger-ui.css", updated)
        start, end = updated.index(b'<style id="code-steward-workspace-nav-style">'), updated.index(b"</nav>") + len(b"</nav>\n")
        self.assertEqual(updated[:start - 1] + updated[end:], original)
        self.assertEqual(asyncio.run(request(install_app, path="/custom/schema.json")),
                         asyncio.run(request(app, path="/custom/schema.json")))

    def test_launcher_adds_navigation_without_starting_runtime(self):
        app = FastAPI()
        server = create_server(app, SimpleNamespace(), server_factory=lambda config: SimpleNamespace(config=config, should_exit=False))
        self.assertFalse(server.should_exit)
        self.assertIs(server.config.app, app)
        html = body(asyncio.run(request(app)))
        self.assertIn(b'href="/shutdown"', html)
        self.assertIn(b"SwaggerUIBundle", html)
        self.assertEqual(app.state.shutdown_coordinator.snapshot()["state"], "idle")

    def test_startup_has_accessible_shutdown_action_even_in_error_dialog(self):
        html = (Path(__file__).resolve().parents[1] / "static" / "startup" / "index.html").read_text(encoding="utf-8")
        actions = html.split('<div class="actions">', 1)[1].split("</div>", 1)[0]
        dialog = html.split('<dialog id="error-dialog"', 1)[1]
        self.assertIn('href="/shutdown">Shut down</a>', actions)
        self.assertIn('href="/shutdown">Shut down</a>', dialog)


if __name__ == "__main__":
    unittest.main()
