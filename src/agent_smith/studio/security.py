"""Local-only access control for Agent Smith Studio.

The studio reads and writes project files and runs a terminal, so a request is served only when it
comes from this machine *and* from the page the user opened with the session token:

* the server binds to a loopback address only (enforced by the CLI);
* the Host header must be a loopback name, which blocks DNS-rebinding attacks;
* ``/api/*`` and ``/ws/*`` need the session token printed in the console (like Jupyter):
  ``Authorization: Bearer <token>`` for HTTP, ``?token=<token>`` for WebSockets (browsers cannot set
  headers on a WebSocket);
* a cross-site Origin is refused, and state-changing HTTP methods also need ``X-Studio: 1`` (CSRF);
* every HTTP response carries ``Referrer-Policy: no-referrer`` so the token in the first URL never leaks.

Static files (the built web app) are public: they contain no data and no secret.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import parse_qs, urlparse

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SECURITY_HEADERS: Tuple[Tuple[bytes, bytes], ...] = (
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"SAMEORIGIN"),  # the studio embeds its own observatory; other sites cannot
)

# Content Security Policy of the studio app (not of /observatory, which keeps its inline scripts):
# only our own scripts run, no third-party connections, frames only from this origin.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'").encode()

Scope = Dict[str, Any]
Receive = Callable[[], Awaitable[Dict[str, Any]]]
Send = Callable[[Dict[str, Any]], Awaitable[None]]


def new_token() -> str:
    return secrets.token_urlsafe(24)


def host_name(host_header: str) -> str:
    """``"127.0.0.1:8790"`` -> ``"127.0.0.1"``, ``"[::1]:8790"`` -> ``"::1"``."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host[1:]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_loopback_origin(origin: str) -> bool:
    try:
        return (urlparse(origin).hostname or "") in LOOPBACK
    except ValueError:
        return False


def token_ok(given: Optional[str], expected: str) -> bool:
    return bool(given) and secrets.compare_digest(given.encode(), expected.encode())


def _headers(scope: Scope) -> Dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _supplied_token(scope: Scope, headers: Dict[str, str]) -> Optional[str]:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    return (query.get("token") or [None])[0]


class GuardMiddleware:
    """Pure ASGI middleware (covers HTTP and WebSocket) implementing the rules above."""

    def __init__(self, app: Callable[..., Awaitable[None]], token: str,
                 protected_prefixes: Iterable[str] = ("/api/", "/ws/")):
        self.app, self.token, self.protected = app, token, tuple(protected_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = _headers(scope)
        if host_name(headers.get("host", "")) not in LOOPBACK:
            return await self._deny(kind, scope, receive, send, 403, "forbidden host")
        if scope["path"].startswith(self.protected):
            origin = headers.get("origin")
            if origin and not is_loopback_origin(origin):
                return await self._deny(kind, scope, receive, send, 403, "forbidden origin")
            if not token_ok(_supplied_token(scope, headers), self.token):
                return await self._deny(kind, scope, receive, send, 401, "missing or wrong session token")
            if kind == "http" and scope["method"] not in SAFE_METHODS and headers.get("x-studio") != "1":
                return await self._deny(kind, scope, receive, send, 403, "missing X-Studio header")
        if kind == "websocket":
            return await self.app(scope, receive, send)

        extra = list(SECURITY_HEADERS)
        if not scope["path"].startswith("/observatory"):
            extra.append((b"content-security-policy", CSP))

        async def send_with_headers(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _deny(kind: str, scope: Scope, receive: Receive, send: Send, status: int, reason: str) -> None:
        if kind == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 4000 + status, "reason": reason})
            return
        body = json.dumps({"error": reason}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                *SECURITY_HEADERS]})
        await send({"type": "http.response.body", "body": body})
