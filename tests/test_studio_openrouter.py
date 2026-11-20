"""OpenRouter tab: picks are global, a picked model shows up in the chat picker, a picked media model is used."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_smith.studio import openrouter  # noqa: E402
from agent_smith.studio.app import create_app  # noqa: E402

H = {"Authorization": "Bearer or-token", "X-Studio": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    (tmp_path / "p").mkdir()
    app = create_app(tmp_path / "p", "or-token", web_dist=None, shell="local", state_dir=tmp_path / "state")
    with TestClient(app, base_url="http://127.0.0.1:8790") as c:
        yield c, app


def test_picks_reach_the_chat_and_the_media_models(client):
    c, app = client
    assert c.get("/api/openrouter/catalog", headers=H).json()["picks"] == {"chat": [], "media": {}}
    r = c.put("/api/openrouter/picks", json={"chat": ["acme/new-model", "acme/new-model", " "], "media": {"image": "acme/painter", "bogus": "x"}}, headers=H)
    assert r.json() == {"chat": ["acme/new-model"], "media": {"image": "acme/painter"}}
    models = c.get("/api/agent/models", headers=H).json()["models"]
    picked = [m for m in models if m["model"] == "acme/new-model"]
    assert picked and picked[0]["provider"] == "openrouter" and picked[0]["paid"] and picked[0]["key_ok"]
    assert app.state.agent.media.default("image") == "acme/painter"
    assert app.state.agent.media.default("speech") != ""                          # not picked: config default
    assert openrouter.picks_path().is_file()                                       # global, outside the project
    assert c.put("/api/openrouter/picks", json={"chat": []}).status_code == 401


def test_catalog_shapes_and_account_numbers(monkeypatch):
    monkeypatch.delenv("AGENT_SMITH_OFFLINE", raising=False)
    openrouter._CACHE.update(models=None, at=0)

    class R:
        def __init__(self, data):
            self.data = data

        def json(self):
            return {"data": self.data}

    def fake_get(url, **kw):
        if url.endswith("/embeddings/models"):
            return R([{"id": "q/emb", "pricing": {"prompt": "0.00000001"}, "architecture": {}}])
        if url.endswith("/models"):
            return R([{"id": "g/painter", "created": 2, "pricing": {"prompt": "0.0000005", "completion": "0.000003", "image_output": "0.00006"},
                       "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image", "text"]}},
                      {"id": "a/chat:free", "created": 1, "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"],
                       "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
                      {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}, "architecture": {}}])
        if url.endswith("/key"):
            return R({"usage": 1.25, "limit": None, "limit_remaining": None, "is_free_tier": False})
        if url.endswith("/credits"):
            return R({"total_credits": 10, "total_usage": 1.25})
        raise AssertionError(url)

    monkeypatch.setattr(openrouter.httpx, "get", fake_get)
    cat = {m["id"]: m for m in openrouter.catalog()}
    assert set(cat) == {"g/painter", "a/chat:free", "q/emb"}                        # variable-price routers left out
    assert cat["g/painter"]["kinds"] == ["image"] and cat["g/painter"]["price"]["image_out"] == 60.0
    assert cat["a/chat:free"]["free"] and cat["a/chat:free"]["tools"] and cat["q/emb"]["kinds"] == ["embeddings"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    acc = openrouter.account()
    assert acc["balance"] == 8.75 and acc["usage"] == 1.25 and "sk-test" not in str(acc)
    openrouter._CACHE.update(models=None, at=0)
