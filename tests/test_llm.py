import pytest

from agent_smith.config import keys_from_env
from agent_smith.llm import Endpoint, KeyPool, LLMClient, LLMError, OpenAICompatProvider
from agent_smith.net import HttpResponse
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
    provider = OpenAICompatProvider("http://unused", "m")
    assert provider._quota_exhausted(HttpResponse(429, '{"error":{"message":"free-models-per-day"}}'))
    assert not provider._quota_exhausted(HttpResponse(429, '{"error":"slow down"}'))
    with pytest.raises(LLMError) as err:
        provider._parse(HttpResponse(429, '{"error":{"message":"Rate limit exceeded: free-models-per-day"}}'), [])
    assert err.value.disable_key


def test_provider_errors_do_not_leak_account_ids():
    """Error texts are stored in committed solution.json files: account identifiers are masked."""
    provider = OpenAICompatProvider("http://unused", "m")
    body = '{"error":{"message":"free-models-per-day","metadata":{}},"user_id":"user_SECRET123"}'
    with pytest.raises(LLMError) as err:
        provider._parse(HttpResponse(429, body), [])
    assert "user_SECRET123" not in str(err.value) and '"user_id":"<redacted>"' in str(err.value)


# ---------------------------------------------------------------- throttling (provider rate limits)
import json as _json  # noqa: E402

from agent_smith.llm.openai_compat import parse_duration, redact_account_ids  # noqa: E402

OK = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
GROQ_TPM = ('{"error":{"message":"Rate limit reached for model `qwen/qwen3.8-27b` in organization `org_01m732g3cnepw8jh1yt8167j4j` '
            'service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 6710, Requested 3895. Please try again in 19.5375s."}}')
GROQ_OTPM = ('{"error":{"message":"Request too large for model `qwen/qwen3.8-27b` in organization `org_01m732g3cnepw8jh1yt8167j4j` '
             'service tier `on_demand` on output tokens per minute (OTPM): Limit 1000, Requested 2000. The request\'s expected output '
             'tokens exceed the enforced limit; reduce max_tokens"}}')


def mock_client(responses, sleeps):
    sent = []

    def handle(method, url, body, headers, timeout, connect_timeout):
        sent.append(_json.loads(body))
        status, text, resp_headers = responses.pop(0)
        return HttpResponse(status, text if isinstance(text, str) else _json.dumps(text), resp_headers)

    provider = OpenAICompatProvider("https://api.groq.com/openai/v1", "qwen/qwen3.8-27b", transport=handle)
    return LLMClient([Endpoint(provider, KeyPool(["k"]))], sleep=sleeps.append), provider, sent


def test_parse_duration():
    assert parse_duration("6.54s") == 6.54 and parse_duration("1m2.5s") == 62.5 and parse_duration("120ms") == 0.12
    assert parse_duration("2") == 2.0 and parse_duration("1h") == 3600 and parse_duration("soon") is None


def test_429_waits_as_long_as_the_body_says_and_hides_the_organization():
    sleeps = []
    c, _, _ = mock_client([(429, GROQ_TPM, {}), (200, OK, {})], sleeps)
    errors = []
    c._on_event = lambda kind, data: errors.append(data.get("error", ""))
    assert c.chat([{"role": "user", "content": "hi"}], 100).text == "ok"
    assert any(19.5 <= s <= 21 for s in sleeps), sleeps                                # "try again in 19.5375s"
    assert "org_01m732" not in "".join(errors) and "org_<redacted>" in errors[0]
    assert redact_account_ids('{"user_id": "user_abc"} org_01m732g3cnepw8jh1yt8167j4j') == '{"user_id": "<redacted>"} org_<redacted>'


def test_output_limit_caps_max_tokens_instead_of_repeating_an_impossible_request():
    sleeps = []
    c, provider, sent = mock_client([(429, GROQ_OTPM, {}), (200, OK, {}), (200, OK, {})], sleeps)
    assert c.chat([{"role": "user", "content": "hi"}], 2000).text == "ok"
    assert [r["max_tokens"] for r in sent] == [2000, 900]                              # capped to 90% of the limit
    c.chat([{"role": "user", "content": "again"}], 2000)
    assert sent[-1]["max_tokens"] == 900 and provider.max_output_cap == 900            # remembered for the model


def test_headers_make_the_client_wait_before_a_request_that_cannot_fit():
    sleeps = []
    c, provider, sent = mock_client([
        (200, OK, {"x-ratelimit-remaining-tokens": "300", "x-ratelimit-reset-tokens": "7.5s", "x-ratelimit-remaining-requests": "99"}),
        (200, OK, {"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-reset-tokens": "1s"}),
    ], sleeps)
    c.chat([{"role": "user", "content": "hi"}], 100)
    assert sleeps == []                                                                # nothing known yet: no wait
    c.chat([{"role": "user", "content": "x" * 3000}], 500)                             # ~1500 tokens > 300 left
    assert len(sent) == 2 and sleeps and 7 <= sleeps[0] <= 7.6                         # waited for the window first
    assert provider.throttle_wait([{"role": "user", "content": "hi"}], 100) == 0.0     # plenty left now


def test_a_wait_beyond_the_time_budget_falls_back_instead():
    import time
    sleeps = []
    c, provider, _ = mock_client([], sleeps)
    provider.tokens_left, provider.tokens_reset_at = 0, time.monotonic() + 300
    with pytest.raises(LLMError, match="beyond the time budget"):
        c.chat([{"role": "user", "content": "hi"}], 100, deadline=time.monotonic() + 60)
    assert sleeps == []


def test_interactive_client_falls_back_instead_of_waiting_long():
    import time
    with FakeLLMServer(["from the backup"]) as backup:
        slow = OpenAICompatProvider("https://api.groq.com/openai/v1", "limited")
        slow.tokens_left, slow.tokens_reset_at = 0, time.monotonic() + 58
        sleeps = []
        c = LLMClient([Endpoint(slow, KeyPool(["a"])), Endpoint(OpenAICompatProvider(backup.url, "m2"), KeyPool(["b"]))], sleep=sleeps.append)
        c.max_wait = 15
        res = c.chat([{"role": "user", "content": "x"}], 10)
    assert res.text == "from the backup" and sleeps == [] and res.model_name == "m2"   # no 58 s wait
