"""OpenRouter tab: picks are global, a picked model shows up in the chat picker, a picked media model is used."""

import json

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
                      {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"}, "architecture": {}},
                      {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}, "architecture": {}}])
        if url.endswith("/key"):
            return R({"usage": 1.25, "limit": None, "limit_remaining": None, "is_free_tier": False})
        if url.endswith("/credits"):
            return R({"total_credits": 10, "total_usage": 1.25})
        raise AssertionError(url)

    monkeypatch.setattr(openrouter.httpx, "get", fake_get)
    cat = {m["id"]: m for m in openrouter.catalog()}
    assert set(cat) == {"g/painter", "q/emb"}                  # variable-price routers, $0 routers and the free tier left out
    assert cat["g/painter"]["kinds"] == ["image"] and cat["g/painter"]["price"]["image_out"] == 60.0
    assert cat["q/emb"]["kinds"] == ["embeddings"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    acc = openrouter.account()
    assert acc["balance"] == 8.75 and acc["usage"] == 1.25 and "sk-test" not in str(acc)
    openrouter._CACHE.update(models=None, at=0)


def test_the_chat_splits_free_and_paid_models(client):
    c, _ = client
    models = c.get("/api/agent/models", headers=H).json()
    paid = [m for m in models["models"] if m["tier"] == "paid"]
    free = [m for m in models["models"] if m["tier"] == "free"]
    assert paid and all(m["provider"] == "openrouter" and m["paid"] and not m["limits"] for m in paid)  # your key
    assert free and all(m["provider"] != "openrouter" and not m["paid"] and m["limits"] for m in free)  # free keys
    assert not any(m["model"].endswith(":free") for m in models["models"])  # OpenRouter's free tier: not used
    assert models["default"] == {"provider": "openrouter", "model": "anthropic/claude-haiku-5.5"}
    assert any(m["model"] == "qwen/qwen3.8-27b" for m in models["models"])            # the Groq model, through your key


def test_live_limits_are_remembered_for_the_free_picker():
    import httpx

    from agent_smith.llm import openai_compat as oc
    prov = oc.OpenAICompatProvider("https://api.example.test/v1/", "m-1")
    prov._learn(httpx.Response(200, headers={"x-ratelimit-remaining-requests": "990", "x-ratelimit-limit-requests": "1000",
                                             "x-ratelimit-reset-requests": "2m", "x-ratelimit-remaining-tokens": "5000",
                                             "x-ratelimit-reset-tokens": "10ms"}))
    import time
    time.sleep(0.05)
    live = oc.live_limits("https://api.example.test/v1", "m-1")
    assert live["requests_left"] == 990 and live["requests_limit"] == 1000
    assert "tokens_left" not in live                                       # its window already reset: stale, dropped
    assert oc.live_limits("https://api.example.test/v1", "other") is None
    oc.LIVE_LIMITS.clear()


def test_a_model_pinned_to_groq_goes_through_openrouter_and_keeps_its_route(monkeypatch):
    import httpx

    from agent_smith.llm.client import build_client, openrouter_route
    assert openrouter_route("openai/gpt-oss-120b@groq") == ("openai/gpt-oss-120b", "groq")
    assert openrouter_route("openai/gpt-oss-120b") is None and openrouter_route("x@groq") is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if "reasoning" in body:  # gpt-oss: "reasoning cannot be disabled"
            return httpx.Response(400, json={"error": {"message": "Reasoning is mandatory for this endpoint"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}, "provider": "Groq"})

    llm = build_client("openai/gpt-oss-120b@groq", "https://openrouter.ai/api/v1", no_fallbacks=True)
    prov = llm.endpoints[0].provider
    prov._client = httpx.Client(transport=httpx.MockTransport(handler))
    assert llm.chat([{"role": "user", "content": "hi"}], max_tokens=10).text == "OK"
    assert [b["model"] for b in sent] == ["openai/gpt-oss-120b"] * 2               # the id OpenRouter knows
    assert all(b["provider"] == {"only": ["groq"], "allow_fallbacks": False} for b in sent)  # route kept on retry
    assert "reasoning" not in sent[-1] and len(llm.endpoints) == 1                  # paid: no fallback


def test_groq_is_in_both_tiers_with_clear_labels(client):
    c, _ = client
    models = c.get("/api/agent/models", headers=H).json()["models"]
    free = [m for m in models if m["provider"] == "groq"]
    paid = [m for m in models if m["model"].endswith("@groq")]
    assert free and all(m["tier"] == "free" for m in free)                 # your free Groq key, its limits
    assert paid and all(m["tier"] == "paid" and m["provider"] == "openrouter" and m["route"] == "groq" for m in paid)
