"""Attachments and voice: uploads into agent_code/uploads, native parts for capable models, descriptions otherwise."""

import base64
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_smith.studio import media as media_mod  # noqa: E402
from agent_smith.studio.agent import AgentService  # noqa: E402
from agent_smith.studio.app import create_app  # noqa: E402
from tests.fake_llm_server import FakeLLMServer  # noqa: E402

TOKEN = "media-token"
H = {"Authorization": f"Bearer {TOKEN}", "X-Studio": "1"}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def make(tmp_path, monkeypatch, script, model="fake-model", provider="fake", paid=False):
    project = tmp_path / "proj"
    project.mkdir()
    srv = FakeLLMServer(script)
    srv.__enter__()
    monkeypatch.setattr(AgentService, "models", staticmethod(lambda: {
        "models": [{"provider": provider, "model": model, "url": srv.url, "key_ok": True, "paid": paid, "key_env": None, "notes": ""}],
        "default": {"provider": provider, "model": model}}))
    app = create_app(project, TOKEN, web_dist=None, shell="local", state_dir=tmp_path / "state")
    return TestClient(app, base_url="http://127.0.0.1:8790"), srv, project, app


def up(client, name, data, mime):
    from urllib.parse import quote
    r = client.post("/api/uploads", content=data, headers={**H, "Content-Type": mime, "X-Filename": quote(name)})
    assert r.status_code == 200, r.text
    return r.json()


def wait(client, rid):
    deadline = time.monotonic() + 60
    while client.get("/api/agent/state", headers=H).json()["runs"][rid]["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    return client.get("/api/agent/state", headers=H).json()["runs"][rid]


def test_uploads_are_stored_in_the_project_and_served_safely(tmp_path, monkeypatch):
    client, srv, project, _ = make(tmp_path, monkeypatch, [])
    with client, srv:
        img = up(client, "screen shot.png", PNG, "image/png")
        assert img["kind"] == "image" and img["path"].startswith("agent_code/uploads/") and (project / img["path"]).read_bytes() == PNG
        assert (project / "agent_code" / ".gitignore").read_text().strip().endswith("*")
        r = client.get(f"/api/uploads/{img['id']}", headers=H)
        assert r.headers["content-type"] == "image/png" and r.content == PNG and "sandbox" in r.headers["content-security-policy"]
        html = up(client, "x.html", b"<script>alert(1)</script>", "text/html")
        r = client.get(f"/api/uploads/{html['id']}", headers=H)
        assert r.headers["content-type"] == "application/octet-stream" and r.headers["content-disposition"].startswith("attachment")
        assert up(client, "../../etc/passwd", b"x", "text/plain")["name"] == "passwd"         # no path from the name
        assert client.post("/api/uploads", content=b"", headers=H).status_code == 400
        assert client.post("/api/uploads", content=PNG).status_code == 401                      # token required


def test_a_text_model_gets_a_description_a_vision_model_the_image(tmp_path, monkeypatch):
    described = []
    monkeypatch.setattr(media_mod.Media, "describe", lambda self, info, tier="paid": described.append((info["name"], tier)) or "A red error dialog: 'Disk full'.")
    client, srv, _, app = make(tmp_path, monkeypatch, ["Thought: ok.\n```python\nfinal_answer('seen')\n```"] * 2, paid=True)
    with client, srv:
        img = up(client, "err.png", PNG, "image/png")
        data = up(client, "data.csv", b"a,b\n1,2\n", "text/csv")
        rid = client.post("/api/agent/runs", json={"message": "what is wrong?", "mode": "ask", "attachments": [img["id"], data["id"]]}, headers=H).json()["id"]
        run = wait(client, rid)
        assert run["status"] == "done" and [a["name"] for a in run["attachments"]] == ["err.png", "data.csv"]
        task = srv.requests[0]["messages"][1]["content"]
        assert isinstance(task, str) and "Disk full" in task and "read_file('agent_code/uploads/" in task   # text-only model
        assert described == [("err.png", "paid")]                    # a paid turn: the paid perceive model
        # the same with a model the catalog says reads images: the image goes in natively
        monkeypatch.setattr(media_mod.Media, "catalog", lambda self: {"fake-model": {"in": ["text", "image"], "out": ["text"], "price": None}})
        rid = client.post("/api/agent/runs", json={"message": "and now?", "mode": "ask", "attachments": [img["id"]]}, headers=H).json()["id"]
        wait(client, rid)
        task = srv.requests[1]["messages"][1]["content"]
        assert isinstance(task, list) and task[1]["type"] == "image_url" and task[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert described == [("err.png", "paid")]                                                         # not described again
        msgs = client.get("/api/agent/state", headers=H).json()["messages"]
        assert msgs[0]["attachments"][0]["kind"] == "image"
        assert client.post("/api/agent/runs", json={"message": "x", "attachments": ["nope"]}, headers=H).status_code == 404


def test_voice_is_transcribed_by_the_configured_model(tmp_path, monkeypatch):
    sent = {}

    def fake_post(self, path, body, timeout=120):
        sent.update(path=path, body=body)
        return {"choices": [{"message": {"content": " Запусти тесты \n"}}]}

    monkeypatch.setattr(media_mod.Media, "_post", fake_post)
    client, srv, _, _ = make(tmp_path, monkeypatch, [])
    with client, srv:
        r = client.post("/api/voice", content=b"RIFF....WAVEfmt ", headers={**H, "Content-Type": "audio/wav", "X-Language": "ru"})
        assert r.json() == {"text": "Запусти тесты"}
        part = sent["body"]["messages"][0]["content"][1]
        assert sent["path"] == "/chat/completions" and part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"
        assert sent["body"]["model"] == media_mod.Media.default(None, "transcribe")
        assert client.post("/api/voice", content=b"", headers=H).status_code == 400


def test_token_estimate_of_a_multimodal_message():
    from agent_smith.llm.base import estimate_messages_tokens
    big = "x" * 3_000_000
    msg = [{"role": "user", "content": [{"type": "text", "text": "abc" * 100}, {"type": "image_url", "image_url": {"url": big}}]}]
    assert estimate_messages_tokens(msg) < 3000            # not the base64 length
