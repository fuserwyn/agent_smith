"""Minimal HTTP for the LLM client, on the standard library only (``http.client`` + ``ssl``).

The agent needs exactly one kind of request, a JSON POST to an OpenAI-compatible ``/chat/completions`` endpoint, so
a third-party HTTP client is not needed. Everything here is small on purpose:

- ``split_url`` / ``hostname_of`` / ``query_params``: the URL handling the project needs (scheme, host, port,
  path; query strings of the local UI server).
- ``HttpResponse``: status, case-insensitive headers, text and ``json()``.
- ``HttpTransport``: sends the request. TLS is verified against the ``certifi`` CA bundle when it is installed (the
  Python builds of uv on macOS may not find the certificates of the OS), else the default CA store.
  Proxy variables (``HTTPS_PROXY``, ``HTTP_PROXY``, ``NO_PROXY``) are honoured; loopback never goes through a proxy.

Errors: ``TransportTimeout`` (connect or read timeout) and ``TransportError`` (DNS, refused connection, TLS, broken
response); the LLM client turns both into retryable ``LLMError``.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
from typing import Any, Callable, Dict, List, NamedTuple, Optional


class TransportError(Exception):
    """The request could not be completed (no HTTP status)."""


class TransportTimeout(TransportError):
    """Connecting or reading took longer than the timeout."""


# ---------------------------------------------------------------- URLs
class SplitURL(NamedTuple):
    scheme: str
    host: str
    port: Optional[int]
    path: str   # at least "/", without the query
    query: str  # without "?"

    @property
    def target(self) -> str:
        """Request target: path and query."""
        return self.path + (f"?{self.query}" if self.query else "")


_URL_RE = re.compile(r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*):)?(?://(?P<netloc>[^/?#]*))?(?P<path>[^?#]*)"
                     r"(?:\?(?P<query>[^#]*))?(?:#.*)?$")


def split_url(url: str) -> SplitURL:
    """'https://user@host:8443/v1/x?a=1' -> SplitURL('https', 'host', 8443, '/v1/x', 'a=1').

    Also accepts a bare path ('/api/trace?path=x'), as received by an HTTP server."""
    m = _URL_RE.match(url.strip())
    if not m:
        raise ValueError(f"not a URL: {url!r}")
    scheme = (m.group("scheme") or "").lower()
    netloc = (m.group("netloc") or "").rsplit("@", 1)[-1]
    host, port = netloc, None
    if netloc.startswith("["):                      # IPv6 literal: [::1]:8080
        end = netloc.find("]")
        host, rest = netloc[1:end], netloc[end + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif ":" in netloc:
        name, _, num = netloc.rpartition(":")
        if num.isdigit():
            host, port = name, int(num)
    path = m.group("path") or "/"
    return SplitURL(scheme, host.lower(), port, path if path.startswith("/") else "/" + path, m.group("query") or "")


def hostname_of(url: str) -> str:
    """Lower-case host of a URL ('' when there is none)."""
    try:
        return split_url(url).host
    except ValueError:
        return ""


def unquote(text: str, plus_is_space: bool = True) -> str:
    """Decode %XX escapes (UTF-8) and, in query strings, '+' as a space."""
    if plus_is_space:
        text = text.replace("+", " ")
    if "%" not in text:
        return text
    out, buf, i = [], bytearray(), 0
    while i < len(text):
        ch = text[i]
        if ch == "%" and re.fullmatch(r"[0-9A-Fa-f]{2}", text[i + 1:i + 3]):
            buf.append(int(text[i + 1:i + 3], 16))
            i += 3
            continue
        if buf:
            out.append(buf.decode("utf-8", "replace"))
            buf = bytearray()
        out.append(ch)
        i += 1
    if buf:
        out.append(buf.decode("utf-8", "replace"))
    return "".join(out)


def query_params(query: str) -> Dict[str, List[str]]:
    """'a=1&b=x%20y&a=2' -> {'a': ['1', '2'], 'b': ['x y']} (blank values dropped, like the usual parsers)."""
    params: Dict[str, List[str]] = {}
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        if value:
            params.setdefault(unquote(key), []).append(unquote(value))
    return params


# ---------------------------------------------------------------- responses
class HttpResponse:
    def __init__(self, status_code: int, text: str = "", headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self.text = text
        self.headers: Dict[str, str] = {k.lower(): v for k, v in (headers or {}).items()}

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.headers.get(name.lower(), default)

    def json(self) -> Any:
        return json.loads(self.text)  # ValueError (JSONDecodeError) on a non-JSON body


# A transport takes (method, url, body, headers, timeout, connect_timeout) and returns an HttpResponse.
Transport = Callable[[str, str, bytes, Dict[str, str], float, float], HttpResponse]


# ---------------------------------------------------------------- the real transport
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # CA bundle; present with every Python HTTP stack, declared in pyproject
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _proxy_for(scheme: str, host: str) -> Optional[SplitURL]:
    if host in _LOOPBACK:
        return None
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    for entry in (e.strip().lower().lstrip(".") for e in no_proxy.split(",")):
        if entry == "*" or (entry and (host == entry or host.endswith("." + entry))):
            return None
    name = "HTTPS_PROXY" if scheme == "https" else "HTTP_PROXY"
    value = os.environ.get(name) or os.environ.get(name.lower())
    return split_url(value if "://" in value else "http://" + value) if value else None


class HttpTransport:
    """One connection per request (an agent step is seconds of LLM time: the TLS handshake does not matter)."""

    def __init__(self) -> None:
        self._ctx: Optional[ssl.SSLContext] = None

    def __call__(self, method: str, url: str, body: bytes, headers: Dict[str, str], timeout: float,
                 connect_timeout: float) -> HttpResponse:
        target = split_url(url)
        if target.scheme not in ("http", "https") or not target.host:
            raise TransportError(f"unsupported URL: {url}")
        port = target.port or (443 if target.scheme == "https" else 80)
        proxy = _proxy_for(target.scheme, target.host)
        if target.scheme == "https" and self._ctx is None:
            self._ctx = _ssl_context()
        path = target.target
        try:
            if proxy and target.scheme == "https":     # CONNECT tunnel, TLS to the real host inside it
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    proxy.host, proxy.port or 80, timeout=connect_timeout, context=self._ctx)
                conn.set_tunnel(target.host, port)
            elif proxy:                                 # plain HTTP through a proxy: absolute request target
                conn = http.client.HTTPConnection(proxy.host, proxy.port or 80, timeout=connect_timeout)
                path = url
            elif target.scheme == "https":
                conn = http.client.HTTPSConnection(target.host, port, timeout=connect_timeout, context=self._ctx)
            else:
                conn = http.client.HTTPConnection(target.host, port, timeout=connect_timeout)
            try:
                conn.connect()
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)      # from now on: the read timeout
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return HttpResponse(resp.status, raw.decode(charset, "replace"), dict(resp.getheaders()))
            finally:
                conn.close()
        except (socket.timeout, TimeoutError) as exc:
            raise TransportTimeout(str(exc) or "timed out") from exc
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise TransportError(str(exc) or exc.__class__.__name__) from exc


def post_json(transport: Transport, url: str, payload: Any, headers: Dict[str, str], timeout: float,
              connect_timeout: float = 10.0) -> HttpResponse:
    body = json.dumps(payload).encode("utf-8")
    return transport("POST", url, body, {**headers, "Content-Type": "application/json",
                                         "Content-Length": str(len(body))}, timeout, connect_timeout)
