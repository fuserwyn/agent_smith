"""Studio terminals: PTY sessions over a WebSocket (local mode here; Docker mode with STUDIO_DOCKER_TESTS=1)."""

import json
import os
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_smith.studio.app import create_app  # noqa: E402

TOKEN = "term-token"
H = {"Authorization": f"Bearer {TOKEN}", "X-Studio": "1"}
WS = "ws://127.0.0.1:8790/ws/terminal/{}?token=" + TOKEN


def read_until(ws, needle: bytes, timeout: float = 10.0) -> bytes:
    """Collect binary frames until `needle` shows up (shells echo input, so look for computed output)."""
    out = b""
    deadline = time.monotonic() + timeout
    while needle not in out:
        assert time.monotonic() < deadline, f"timeout waiting for {needle!r}; got {out[-300:]!r}"
        msg = ws.receive()
        if msg.get("bytes"):
            out += msg["bytes"]
        elif msg.get("text") and json.loads(msg["text"]).get("type") == "exit":
            break
    return out


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", "must-not-leak")
    app = create_app(tmp_path, TOKEN, web_dist=None, shell="local")
    with TestClient(app, base_url="http://127.0.0.1:8790") as c:
        yield c, tmp_path


def test_shell_runs_in_the_project_and_answers(local):
    client, root = local
    sid = client.post("/api/terminals", json={"cols": 80, "rows": 24}, headers=H).json()["id"]
    with client.websocket_connect(WS.format(sid)) as ws:
        ws.send_text(json.dumps({"type": "input", "data": "echo answer=$((6*7)); pwd\n"}))
        out = read_until(ws, b"answer=42")
        here = str(root.resolve()).encode()
        if here not in out.split(b"answer=42", 1)[1]:
            out += read_until(ws, here)
        assert here in out.split(b"answer=42", 1)[1]


def test_resize_reaches_the_pty(local):
    client, _ = local
    sid = client.post("/api/terminals", json={}, headers=H).json()["id"]
    with client.websocket_connect(WS.format(sid)) as ws:
        ws.send_text(json.dumps({"type": "resize", "cols": 123, "rows": 45}))
        ws.send_text(json.dumps({"type": "input", "data": "stty size\n"}))
        read_until(ws, b"45 123")


def test_keys_never_reach_the_shell(local):
    client, _ = local
    sid = client.post("/api/terminals", json={}, headers=H).json()["id"]
    with client.websocket_connect(WS.format(sid)) as ws:
        ws.send_text(json.dumps({"type": "input", "data": "echo key=${FAKE_PROVIDER_API_KEY:-absent} tok=${STUDIO_TOKEN:-absent}\n"}))
        read_until(ws, b"key=absent tok=absent")


def test_reconnect_replays_scrollback_and_exit_is_reported(local):
    client, _ = local
    sid = client.post("/api/terminals", json={}, headers=H).json()["id"]
    with client.websocket_connect(WS.format(sid)) as ws:
        ws.send_text(json.dumps({"type": "input", "data": "echo first=$((1+1))\n"}))
        read_until(ws, b"first=2")
    with client.websocket_connect(WS.format(sid)) as ws:  # page reload
        assert b"first=2" in read_until(ws, b"first=2", timeout=3)
        ws.send_text(json.dumps({"type": "input", "data": "exit 3\n"}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            msg = ws.receive()
            if msg.get("text"):
                assert json.loads(msg["text"]) == {"type": "exit", "code": 3}
                break
        else:
            pytest.fail("no exit message")
    st = client.get("/api/terminals", headers=H).json()
    assert st["mode"] == "local" and [s["alive"] for s in st["sessions"]] == [False]


def test_kill_and_limits(local):
    client, _ = local
    ids = [client.post("/api/terminals", json={}, headers=H).json()["id"] for _ in range(8)]
    assert client.post("/api/terminals", json={}, headers=H).status_code == 429
    for sid in ids:
        assert client.delete(f"/api/terminals/{sid}", headers=H).status_code == 200
    assert client.get("/api/terminals", headers=H).json()["sessions"] == []


def test_unknown_terminal_and_auth(local):
    client, _ = local
    assert client.post("/api/terminals", json={}).status_code == 401
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(WS.format("nope")) as ws:
            ws.receive()
    assert e.value.code == 4404


def test_docker_mode_explains_when_docker_is_missing(tmp_path, monkeypatch):
    app = create_app(tmp_path, TOKEN, web_dist=None, shell="docker")
    monkeypatch.setattr(app.state.terminals.container, "available", lambda: "Docker is not running")
    with TestClient(app, base_url="http://127.0.0.1:8790") as c:
        r = c.post("/api/terminals", json={}, headers=H)
        assert r.status_code == 503 and "not running" in r.json()["error"]
        assert c.get("/api/terminals", headers=H).json()["available"] is False


@pytest.mark.skipif(os.environ.get("STUDIO_DOCKER_TESTS") != "1", reason="set STUDIO_DOCKER_TESTS=1 (starts a container)")
def test_docker_shell_sees_only_the_project(tmp_path):
    (tmp_path / "hello.txt").write_text("from the host\n")
    app = create_app(tmp_path, TOKEN, web_dist=None, shell="docker")
    with TestClient(app, base_url="http://127.0.0.1:8790") as c:
        sid = c.post("/api/terminals", json={}, headers=H).json()["id"]
        with c.websocket_connect(WS.format(sid)) as ws:
            ws.send_text(json.dumps({"type": "input", "data": "cat hello.txt; echo made > from_container.txt; echo where=$(pwd)\n"}))
            out = read_until(ws, b"where=/workspace", timeout=60)
            assert b"from the host" in out
        assert (tmp_path / "from_container.txt").read_text() == "made\n"
        name = app.state.terminals.container.name
    import subprocess
    assert subprocess.run(["docker", "inspect", name], capture_output=True).returncode != 0  # removed on shutdown
