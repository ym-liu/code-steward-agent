"""Add application navigation to the existing Swagger HTML without replacing it."""
import re


_BODY = re.compile(rb"<body\b[^>]*>", re.IGNORECASE)
_NAV_ID = b'id="code-steward-workspace-nav"'
_NAVIGATION = b"""
<style id="code-steward-workspace-nav-style">
body:has(>#code-steward-workspace-nav){padding-top:60px!important}
#code-steward-workspace-nav{position:fixed;inset:0 0 auto;z-index:10000;height:60px;
box-sizing:border-box;display:flex;align-items:center;justify-content:space-between;
gap:12px;padding:0 24px;background:#f6f8f7;border-bottom:1px solid #dce6e0;
color:#18362e;font:500 14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
#code-steward-workspace-nav a{box-sizing:border-box;display:inline-flex;
align-items:center;justify-content:center;min-height:40px;text-decoration:none}
#code-steward-workspace-nav .cs-startup{color:#18362e;font-weight:600}
#code-steward-workspace-nav .cs-shutdown{gap:8px;padding:8px 16px;border:1px solid #b8cfc2;
border-radius:8px;background:#fff;color:#244f3f;white-space:nowrap}
#code-steward-workspace-nav a:hover{color:#155c43;background:#e9f1ec}
#code-steward-workspace-nav a:focus-visible{outline:3px solid #297760;outline-offset:3px}
#code-steward-workspace-nav svg{width:17px;height:17px;flex-shrink:0}
@media(max-width:480px){#code-steward-workspace-nav{padding:0 12px;gap:8px;font-size:12px}
#code-steward-workspace-nav .cs-shutdown{padding:8px 12px}}
</style>
<nav id="code-steward-workspace-nav" aria-label="Application controls">
  <a class="cs-startup" href="/startup">Code Steward <span aria-hidden="true">&nbsp;/&nbsp;</span> Startup</a>
  <a class="cs-shutdown" href="/shutdown"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 2v10M6 5a9 9 0 1 0 12 0"/></svg>Shut down</a>
</nav>
"""


class WorkspaceNavigationMiddleware:
    """Decorate only successful, uncompressed GET /docs HTML responses.

    All other requests pass through untouched. The original Swagger HTML and
    scripts remain in place; visiting the shutdown page does not start shutdown.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "GET"
                or scope.get("path") != "/docs"):
            await self.app(scope, receive, send)
            return

        start = None
        bodies = []

        async def navigation_send(message):
            nonlocal start
            if message["type"] == "http.response.start":
                headers = {key.lower(): value.lower() for key, value in message.get("headers", [])}
                content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip()
                if (message["status"] == 200 and content_type == b"text/html"
                        and headers.get(b"content-encoding", b"identity") == b"identity"):
                    start = message
                    return
            if start is not None and message["type"] == "http.response.body":
                bodies.append(message)
                if message.get("more_body", False):
                    return
                original = b"".join(part.get("body", b"") for part in bodies)
                opening = _BODY.search(original)
                if opening is None or _NAV_ID in original:
                    await send(start)
                    for part in bodies:
                        await send(part)
                else:
                    body = original[:opening.end()] + _NAVIGATION + original[opening.end():]
                    # Entity hashes describe the original HTML and cannot survive insertion.
                    stale = {b"content-length", b"etag", b"content-md5", b"digest", b"content-digest"}
                    headers = [(key, value) for key, value in start.get("headers", []) if key.lower() not in stale]
                    headers.append((b"content-length", str(len(body)).encode("ascii")))
                    await send({**start, "headers": headers})
                    await send({**message, "body": body})
                start = None
                bodies.clear()
                return
            await send(message)

        await self.app(scope, receive, navigation_send)


def install_workspace_navigation(app):
    """Register once through the optional launcher, before the app starts."""
    if not any(item.cls is WorkspaceNavigationMiddleware for item in app.user_middleware):
        app.add_middleware(WorkspaceNavigationMiddleware)
