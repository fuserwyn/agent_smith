"""run_app & co: the chat agent builds a project folder with Docker and serves it on a free local port."""

import os
import re
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_smith.studio.agent import AgentService  # noqa: E402
from agent_smith.studio.app import create_app  # noqa: E402
from agent_smith.studio.apps import AppManager, exposed_port  # noqa: E402
from agent_smith.studio.workspace import Workspace  # noqa: E402
from tests.fake_llm_server import FakeLLMServer  # noqa: E402

TOKEN = "apps-token"
H = {"Authorization": f"Bearer {TOKEN}", "X-Studio": "1"}


class FakeDocker:
    """docker build/run/inspect/logs/rm; 'run' serves a tiny HTTP app on the published host port."""

    def __init__(self):
        self.calls, self.servers = [], {}

    def __call__(self, *args, timeout=0):
        self.calls.append(args)
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")  # noqa: E731
        if args[0] == "info":
            return ok("29.0")
        if args[0] == "build":
            folder = args[-1]
            if "FAIL" in open(os.path.join(folder, "Dockerfile")).read():
                return subprocess.CompletedProcess(args, 1, "", "ERROR: failed to solve: pip install nope")
            return ok()
        if args[0] == "run":
            name = args[args.index("--name") + 1]
            port = int(re.search(r"127\.0\.0\.1:(\d+):", " ".join(args)).group(1))

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_GET(self):  # noqa: N802
                    body = b'{"hello": "world"}' if self.path == "/" else b"not found"
                    self.send_response(200 if self.path == "/" else 404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)

                def do_POST(self):  # noqa: N802
                    data = self.rfile.read(int(self.headers["Content-Length"]))
                    self.send_response(201)
                    self.end_headers()
                    self.wfile.write(b"got " + data)

            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.servers[name] = srv
            return ok("container-id")
        if args[0] == "inspect":
            return ok("true" if args[-1] in self.servers else "false")
        if args[0] == "logs":
            return ok("Serving on 0.0.0.0:8000\nGET / 200")
        if args[0] == "rmi":
            return ok()
        if args[0] == "rm":
            srv = self.servers.pop(args[-1], None)
            if srv:
                srv.shutdown()
                srv.server_close()
            return ok()
        raise AssertionError(args)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    app = root / "agent_code" / "apps" / "hello"
    app.mkdir(parents=True)
    (app / "app.py").write_text("print('hi')\n")
    (app / "Dockerfile").write_text("FROM python:3.11-slim\nCOPY . .\nEXPOSE 8000\nCMD [\"python\", \"app.py\"]\n")
    (root / "broken").mkdir()
    (root / "broken" / "Dockerfile").write_text("FROM python:3.11-slim\nRUN FAIL\n")
    return root


@pytest.fixture
def manager(project):
    events = []
    m = AppManager(Workspace(project), events.append, docker=FakeDocker())
    yield m, events
    m.stop_all()


def test_run_call_logs_stop(manager):
    m, events = manager
    out = m.call("run_app", {"path": "agent_code/apps/hello"}, "turn-1")
    assert not out.is_error, out.text
    url = re.search(r"http://127\.0\.0\.1:(\d+)", out.text).group(0)
    assert "First response: GET / -> 200" in out.text and "Serving on" in out.text
    assert urllib.request.urlopen(url).read() == b'{"hello": "world"}'          # really reachable from this machine
    run = next(c for c in m.docker.calls if c[0] == "run")
    assert "--cap-drop" in run and "no-new-privileges" in run and f"127.0.0.1:{url.rsplit(':', 1)[1]}:8000" in run
    post = m.call("call_app", {"path": "agent_code/apps/hello", "route": "/items", "method": "POST", "body": {"a": 1}}, None)
    assert "-> 201" in post.text and 'got {"a": 1}' in post.text
    assert "-> 404" in m.call("call_app", {"path": "agent_code/apps/hello", "route": "/nope"}, None).text
    assert "Serving on" in m.call("app_logs", {"path": "/workspace/agent_code/apps/hello"}, None).text  # container path ok
    assert "Serving on" in m.call("app_logs", {"path": str(m.ws.root / "agent_code/apps/hello")}, None).text  # host path ok
    [listed] = m.list()
    assert listed["status"] == "running" and listed["run"] == "turn-1" and listed["url"] == url
    assert [e["app"]["status"] for e in events] == ["starting", "running"]
    assert not m.call("stop_app", {"path": "agent_code/apps/hello"}, None).is_error
    assert m.list()[0]["status"] == "stopped"
    with pytest.raises(OSError):
        urllib.request.urlopen(url, timeout=2)
    assert m.call("call_app", {"path": "agent_code/apps/hello"}, None).is_error


def test_restart_keeps_the_url_and_a_requested_port_is_checked(manager):
    m, _ = manager
    first = re.search(r"http://\S+", m.call("run_app", {"path": "agent_code/apps/hello"}, None).text).group(0)
    second = re.search(r"http://\S+", m.call("run_app", {"path": "agent_code/apps/hello"}, None).text).group(0)
    assert first == second
    busy = int(first.rsplit(":", 1)[1])
    m.call("stop_app", {"path": "agent_code/apps/hello"}, None)
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        taken = s.getsockname()[1]
        out = m.call("run_app", {"path": "agent_code/apps/hello", "port": taken}, None)
        assert out.is_error and "already in use" in out.text
    assert busy > 0


def test_errors_are_explained(manager, project):
    m, _ = manager
    out = m.call("run_app", {"path": "broken"}, None)
    assert out.is_error and "docker build failed" in out.text and "pip install nope" in out.text
    assert m.list()[0]["status"] == "failed"
    (project / "nodocker").mkdir()
    assert "Dockerfile not found" in m.call("run_app", {"path": "nodocker"}, None).text
    assert m.call("run_app", {"path": "../outside"}, None).is_error                     # workspace jail
    assert m.call("call_app", {"path": "agent_code/apps/hello", "route": "http://evil.example/"}, None).is_error
    assert exposed_port("FROM x\nexpose 5000\n") == 5000 and exposed_port("FROM x\n") is None


def test_the_chat_agent_writes_a_project_and_serves_it(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    step = lambda code: f"Thought: next.\n```python\n{code}\n```"  # noqa: E731
    srv = FakeLLMServer([
        step("print(edit_file('agent_code/apps/hi/app.py', '', 'print(1)\\n'))\n"
             "print(edit_file('agent_code/apps/hi/Dockerfile', '', 'FROM python:3.11-slim\\nEXPOSE 8000\\n'))"),
        step("print(run_app('agent_code/apps/hi'))"),
        step("print(call_app('agent_code/apps/hi', '/'))"),
        step("final_answer('Running.')"),
    ])
    srv.__enter__()
    monkeypatch.setattr(AgentService, "models", staticmethod(lambda: {
        "models": [{"provider": "fake", "model": "fake-model", "url": srv.url, "key_ok": True, "paid": False, "key_env": None, "notes": ""}],
        "default": {"provider": "fake", "model": "fake-model"}}))
    app = create_app(project, TOKEN, web_dist=None, shell="local", state_dir=tmp_path / "state")
    app.state.agent.apps.docker = FakeDocker()
    app.state.agent.apps._docker_ok = None
    client = TestClient(app, base_url="http://127.0.0.1:8790")
    with client, srv:
        run = client.post("/api/agent/runs", json={"message": "make a hello server", "mode": "agent"}, headers=H).json()
        deadline = time.monotonic() + 60
        while client.get("/api/agent/state", headers=H).json()["runs"][run["id"]]["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert "run_app(" in srv.requests[0]["messages"][0]["content"]              # offered like an MCP tool
        assert "```python\n```" in srv.requests[0]["stop"]                        # the empty-block loop is cut short
        assert "is running at http://127.0.0.1:" in srv.requests[2]["messages"][-1]["content"]
        assert '-> 200' in srv.requests[3]["messages"][-1]["content"]
        [listed] = client.get("/api/apps", headers=H).json()["apps"]
        assert listed["path"] == "agent_code/apps/hi" and listed["status"] == "running"
        assert "Serving on" in client.get(f"/api/apps/{listed['id']}/logs", headers=H).json()["logs"]
        assert client.post(f"/api/apps/{listed['id']}/stop", headers=H).json()["status"] == "stopped"
        assert client.post("/api/apps/nope/stop", headers=H).status_code == 404


@pytest.mark.skipif(os.environ.get("STUDIO_DOCKER_TESTS") != "1", reason="real Docker: STUDIO_DOCKER_TESTS=1")
def test_real_docker_app(tmp_path):
    app = tmp_path / "proj" / "agent_code" / "apps" / "real"
    app.mkdir(parents=True)
    (app / "server.py").write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'hello from docker')\n"
        "print('listening', flush=True)\nHTTPServer(('0.0.0.0', 8000), H).serve_forever()\n")
    (app / "Dockerfile").write_text("FROM python:3.11-slim\nWORKDIR /app\nCOPY server.py .\nEXPOSE 8000\nCMD [\"python\", \"server.py\"]\n")
    m = AppManager(Workspace(tmp_path / "proj"), lambda e: None)
    try:
        out = m.call("run_app", {"path": "agent_code/apps/real"}, None)
        assert not out.is_error, out.text
        assert "hello from docker" in m.call("call_app", {"path": "agent_code/apps/real"}, None).text
        assert "listening" in m.call("app_logs", {"path": "agent_code/apps/real"}, None).text
    finally:
        m.stop_all()
