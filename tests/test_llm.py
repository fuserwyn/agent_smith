import pytest

from agent_smith.config import keys_from_env
from agent_smith.llm import Endpoint, KeyPool, LLMClient, LLMError, OpenAICompatProvider
from tests.fake_llm_server import FakeLLMServer


def client(url, keys, **kw):
    return LLMClient([Endpoint(OpenAICompatProvider(url, "m"), KeyPool(keys))], sleep=lambda s: None, **kw)


def test_keys_from_env(monkeypatch):
    monkeypatch.setenv("X_API_KEY", "a")
    monkeypatch.setenv("X_API_KEY_2", "b")
    monkeypatch.setenv("X_API_KEYS", "c, a")
    assert keys_from_env("X_API_KEY") == ["a", "b", "c"]


def test_success_records_usage_and_stop():
    with FakeLLMServer(["Thought\n```python\nprint(1)\n```<end_code>\nObservation: fake"]) as srv:
        res = client(srv.url, ["k1"]).chat([{"role": "user", "content": "hi"}], 100, stop=["<end_code>"])
    assert res.text.endswith("```") and "Observation" not in res.text
    assert res.retries == 0 and res.requests == 1 and res.input_tokens > 0 and res.api_url == srv.url


def test_rate_limit_rotates_key_and_counts_retries():
    with FakeLLMServer([{"status": 429}, {"status": 500}, "ok"]) as srv:
        c = client(srv.url, ["key-A", "key-B"])
        res = c.chat([{"role": "user", "content": "hi"}], 10)
        assert res.text == "ok" and res.retries == 2 and res.requests == 3 and c.total_requests == 3
        assert srv.auth_headers[0] != srv.auth_headers[1]  # rotated after 429


def test_invalid_key_disabled_then_next_key():
    with FakeLLMServer([{"status": 401}, "ok"]) as srv:
        pool = KeyPool(["bad", "good"])
        c = LLMClient([Endpoint(OpenAICompatProvider(srv.url, "m"), pool)], sleep=lambda s: None)
        assert c.chat([{"role": "user", "content": "x"}], 10).text == "ok"
        assert pool.available() == 1 and srv.auth_headers[1] == "Bearer good"


def test_fallback_to_second_provider():
    with FakeLLMServer([{"status": 400}]) as bad, FakeLLMServer(["from fallback"]) as good:
        c = LLMClient([Endpoint(OpenAICompatProvider(bad.url, "m1"), KeyPool(["a"])),
                       Endpoint(OpenAICompatProvider(good.url, "m2"), KeyPool(["b"]))], sleep=lambda s: None)
        res = c.chat([{"role": "user", "content": "x"}], 10)
    assert res.text == "from fallback" and res.model_name == "m2"


def test_all_attempts_fail_raises():
    with FakeLLMServer([{"status": 503}] * 10) as srv:
        with pytest.raises(LLMError):
            client(srv.url, ["k"], max_attempts=3).chat([{"role": "user", "content": "x"}], 10)



def test_daily_quota_is_detected_and_key_disabled():
    """A per-day 429 must disable the key at once instead of being retried until the time budget is gone."""
    import httpx

    provider = OpenAICompatProvider("http://unused", "m")
    assert provider._quota_exhausted(httpx.Response(429, text='{"error":{"message":"free-models-per-day"}}'))
    assert not provider._quota_exhausted(httpx.Response(429, text='{"error":"slow down"}'))
    with pytest.raises(LLMError) as err:
        provider._parse(httpx.Response(429, text='{"error":{"message":"Rate limit exceeded: free-models-per-day"}}'), [])
    assert err.value.disable_key
