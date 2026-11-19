"""Agent Smith Studio server: access control first (local only, session token, CSRF), then the basic API."""

import pytest

pytest.importorskip("fastapi")  # studio extra: uv sync --extra studio
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from agent_smith.studio import cli  # noqa: E402
from agent_smith.studio.app import create_app  # noqa: E402
from agent_smith.studio.security import host_name, is_loopback_origin  # noqa: E402

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\n")
    return TestClient(create_app(tmp_path, TOKEN, web_dist=None), base_url="http://127.0.0.1:8790")


def test_api_needs_the_session_token(client):
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/api/health", headers=AUTH)
    assert r.status_code == 200 and r.json()["app"] == "agent-smith-studio"
    assert client.get(f"/api/health?token={TOKEN}").status_code == 200  # query form (used by WebSockets)


def test_foreign_host_and_origin_are_refused(client):
    assert client.get("/api/health", headers={**AUTH, "Host": "evil.example"}).status_code == 403  # DNS rebinding
    assert client.get("/", headers={"Host": "evil.example:8790"}).status_code == 403
    assert client.get("/api/health", headers={**AUTH, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/health", headers={**AUTH, "Origin": "http://localhost:5173"}).status_code == 200  # dev server


def test_state_changing_requests_need_the_csrf_header(client):
    assert client.post("/api/health", headers=AUTH).status_code == 403
    assert client.post("/api/health", headers={**AUTH, "X-Studio": "1"}).status_code == 405  # guard passed, no such route


def test_security_headers_and_public_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "Agent Smith Studio" in r.text  # served without token: no data in it
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-frame-options"] == "DENY"


def test_session_describes_workspace_and_docker(client, tmp_path):
    data = client.get("/api/session", headers=AUTH).json()
    assert data["workspace"]["name"] == tmp_path.name and data["workspace"]["git"] is False
    assert set(data["docker"]) == {"installed", "running"}


def test_websocket_without_token_is_closed(client):
    for url, code in [("ws://127.0.0.1:8790/ws/terminal", 4401), ("ws://evil.example/ws/terminal?token=" + TOKEN, 4403)]:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(url) as ws:
                ws.receive_text()
        assert exc.value.code == code


def test_host_and_origin_helpers():
    assert host_name("127.0.0.1:8790") == "127.0.0.1" and host_name("[::1]:8790") == "::1" and host_name("LOCALHOST") == "localhost"
    assert is_loopback_origin("http://localhost:5173") and not is_loopback_origin("http://127.0.0.1.evil.example")


def test_cli_refuses_remote_binding_and_missing_folder(tmp_path, capsys):
    assert cli.main([str(tmp_path), "--host", "0.0.0.0", "--no-browser"]) == 2
    assert "only serves on this machine" in capsys.readouterr().err
    assert cli.main([str(tmp_path / "nope"), "--no-browser"]) == 2
