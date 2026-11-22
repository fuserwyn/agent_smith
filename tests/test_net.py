"""Standard-library HTTP used by the LLM client: URL handling, transport, errors, proxies."""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_smith import net
from agent_smith.net import (HttpResponse, HttpTransport, TransportError, TransportTimeout, hostname_of, post_json,
                             query_params, split_url)
from tests.fake_llm_server import FakeLLMServer


def test_split_url():
    u = split_url("https://user@API.Groq.com:8443/openai/v1/chat?a=1&b=2#frag")
    assert (u.scheme, u.host, u.port, u.path, u.query) == ("https", "api.groq.com", 8443, "/openai/v1/chat", "a=1&b=2")
    assert u.target == "/openai/v1/chat?a=1&b=2"
    assert split_url("http://127.0.0.1:11434/v1").port == 11434
    assert split_url("http://[::1]:8080/x").host == "::1" and split_url("http://[::1]:8080/x").port == 8080
    assert split_url("https://integrate.api.nvidia.com").path == "/"
    bare = split_url("/api/trace?path=runs%2Fa+b.jsonl")
    assert (bare.host, bare.path, bare.query) == ("", "/api/trace", "path=runs%2Fa+b.jsonl")


def test_hostname_and_query():
    assert hostname_of("https://openrouter.ai/api/v1") == "openrouter.ai"
    assert hostname_of("not a url") == ""
    assert query_params("path=runs%2Fa+b.jsonl&default=1&default=2&empty=") == {
        "path": ["runs/a b.jsonl"], "default": ["1", "2"]}
    assert query_params("q=%E2%9C%93") == {"q": ["✓"]}
    assert query_params("q=100%") == {"q": ["100%"]}         # a lone % stays as is


def test_response_headers_are_case_insensitive():
    r = HttpResponse(429, '{"a": 1}', {"Retry-After": "3", "X-RateLimit-Remaining-Tokens": "10"})
    assert r.headers.get("retry-after") == "3" and r.header("X-RATELIMIT-REMAINING-TOKENS") == "10"
    assert r.json() == {"a": 1}
    with pytest.raises(ValueError):
        HttpResponse(200, "<html>").json()


def test_post_json_against_a_local_server():
    with FakeLLMServer(["hello"]) as srv:
        resp = post_json(HttpTransport(), srv.url + "/chat/completions",
                         {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                         {"Authorization": "Bearer k"}, timeout=10)
        assert resp.status_code == 200 and resp.json()["choices"][0]["message"]["content"] == "hello"
        assert srv.auth_headers == ["Bearer k"]


def test_read_timeout_is_a_transport_timeout():
    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            time.sleep(2)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(TransportTimeout):
            post_json(HttpTransport(), f"http://127.0.0.1:{srv.server_address[1]}/x", {}, {}, timeout=0.3)
    finally:
        srv.shutdown()


def test_connection_errors_are_transport_errors():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                               # nothing listens on this port any more
    with pytest.raises(TransportError):
        post_json(HttpTransport(), f"http://127.0.0.1:{port}/x", {}, {}, timeout=2)
    with pytest.raises(TransportError):
        post_json(HttpTransport(), "ftp://example.org/x", {}, {}, timeout=2)


def test_proxy_settings(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("NO_PROXY", "internal.example, .corp")
    assert net._proxy_for("https", "api.groq.com").host == "proxy.local"
    assert net._proxy_for("https", "api.groq.com").port == 3128
    assert net._proxy_for("https", "internal.example") is None
    assert net._proxy_for("https", "llm.corp") is None
    assert net._proxy_for("http", "127.0.0.1") is None       # loopback never goes through a proxy
    assert net._proxy_for("http", "api.groq.com") is None    # HTTP_PROXY not set


def test_loopback_works_with_a_proxy_configured(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")    # would fail if it were used
    with FakeLLMServer(["ok"]) as srv:
        assert post_json(HttpTransport(), srv.url + "/chat/completions", {"messages": []}, {}, timeout=10).status_code == 200


def test_sandbox_policy_is_loaded_and_fails_closed(tmp_path):
    """The worker's deny lists live in worker_policy.json; a broken policy must stop the worker, not disable it."""
    import importlib.util
    import json

    from agent_smith.sandbox import host
    spec = importlib.util.spec_from_file_location("worker_under_test", host.WORKER_PATH)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    assert {"os", "subprocess", "socket", "http"} <= worker.HARD_DENY_MODULES
    assert "socket." in worker.DENIED_EVENT_PREFIXES
    bad = tmp_path / "policy.json"
    bad.write_text(json.dumps({"hard_deny_modules": [], "denied_event_prefixes": ["x."]}))
    with pytest.raises(ValueError):
        worker._load_policy(str(bad))
    with pytest.raises(OSError):
        worker._load_policy(str(tmp_path / "missing.json"))
