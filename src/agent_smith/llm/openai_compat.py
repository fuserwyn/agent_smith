"""OpenAI-compatible chat-completions provider (OpenRouter, Groq, Gemini, Mistral, Ollama...)."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

import httpx

from agent_smith.llm.base import LLMError, LLMResponse, Message, estimate_messages_tokens, estimate_tokens

# Provider error bodies can carry account identifiers (OpenRouter: "user_id":"user_..."); error texts end up in
# solution.json files that are committed, so the identifiers are masked before they are stored anywhere.
_ACCOUNT_ID_RE = re.compile(r'("(?:user_id|organization_id|org_id|account_id)"\s*:\s*")[^"]*(")')
_INLINE_ID_RE = re.compile(r"\b(org|user|acct|proj)_[A-Za-z0-9]{8,}")  # Groq writes "organization `org_01…`" in prose


def redact_account_ids(text: str) -> str:
    return _INLINE_ID_RE.sub(r"\1_<redacted>", _ACCOUNT_ID_RE.sub(r"\1<redacted>\2", text))


# ---------------------------------------------------------------- rate limits (throttling)
_DUR_RE = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")
_TRY_AGAIN_RE = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?(?:h|ms|m|s))+)", re.I)
_TOO_LARGE_RE = re.compile(r"Request too large.*?\((OTPM|TPM)\):\s*Limit\s+(\d+),\s*Requested\s+(\d+)", re.I | re.S)


def parse_duration(text: Optional[str]) -> Optional[float]:
    """'6.54s', '1m2.5s', '120ms', '2' (seconds) -> seconds."""
    if not text:
        return None
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    m = _DUR_RE.match(text)
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


class OpenAICompatProvider:
    def __init__(self, base_url: str, model: str, extra_headers: Optional[Dict[str, str]] = None,
                 transport: Optional[httpx.BaseTransport] = None, extra_body: Optional[Dict[str, Any]] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_headers = extra_headers or {}
        # e.g. {"reasoning": {"enabled": false}}: thinking tokens count toward the output budget.
        self.extra_body = dict(extra_body or {})
        self._client = httpx.Client(transport=transport)
        # what the provider told us about its limits (x-ratelimit-* headers, 429 bodies): used to wait *before*
        # a request that cannot fit instead of collecting 429s
        self.tokens_left: Optional[int] = None
        self.tokens_reset_at = 0.0
        self.requests_left: Optional[int] = None
        self.requests_reset_at = 0.0
        self.max_output_cap: Optional[int] = None  # per-request output limit learned from "Request too large"

    def throttle_wait(self, messages: List[Message], max_tokens: int) -> float:
        """Seconds to wait before this request fits the provider's current window (0: send now)."""
        now = time.monotonic()
        wait = 0.0
        if self.requests_left is not None and self.requests_left <= 0 and self.requests_reset_at > now:
            wait = self.requests_reset_at - now
        if self.tokens_left is not None and self.tokens_reset_at > now:
            need = estimate_messages_tokens(messages) + min(max_tokens, self.max_output_cap or max_tokens)
            if need > self.tokens_left:
                wait = max(wait, self.tokens_reset_at - now)
        return round(wait, 2)

    def _learn(self, resp: httpx.Response) -> None:
        h, now = resp.headers, time.monotonic()
        try:
            if h.get("x-ratelimit-remaining-tokens") is not None:
                self.tokens_left = int(float(h["x-ratelimit-remaining-tokens"]))
                self.tokens_reset_at = now + (parse_duration(h.get("x-ratelimit-reset-tokens")) or 60.0)
            if h.get("x-ratelimit-remaining-requests") is not None:
                self.requests_left = int(float(h["x-ratelimit-remaining-requests"]))
                self.requests_reset_at = now + (parse_duration(h.get("x-ratelimit-reset-requests")) or 60.0)
        except (TypeError, ValueError):
            pass

    def complete(self, messages: List[Message], *, api_key: Optional[str], max_tokens: int,
                 stop: Optional[List[str]], temperature: float, timeout: float) -> LLMResponse:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if self.max_output_cap:
            max_tokens = min(max_tokens, self.max_output_cap)
        payload = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature}
        if stop:
            payload["stop"] = stop[:4]  # many providers accept at most 4 stop sequences
        payload.update(self.extra_body)
        resp = self._post(payload, headers, timeout)
        if resp.status_code in (400, 422) and self.extra_body:
            # Some models reject provider-specific options (e.g. "reasoning cannot be disabled"):
            # drop them for this model and retry once.
            for key in self.extra_body:
                payload.pop(key, None)
            self.extra_body = {}
            resp = self._post(payload, headers, timeout)
        self._learn(resp)
        result = self._parse(resp, messages)
        # Providers that ignore `stop` would let the model invent an Observation: cut it client-side.
        for s in stop or []:
            if s in result.text:
                result.text = result.text.split(s, 1)[0]
        return result

    def _post(self, payload: Dict[str, Any], headers: Dict[str, str], timeout: float) -> httpx.Response:
        try:
            return self._client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers,
                                     timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)))
        except httpx.TimeoutException as exc:
            raise LLMError(f"request timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"network error: {exc}", retryable=True) from exc

    @staticmethod
    def _retry_after(resp: httpx.Response) -> Optional[float]:
        value = resp.headers.get("retry-after")
        try:
            return float(value) if value else None
        except ValueError:
            return None

    @staticmethod
    def _quota_exhausted(resp: httpx.Response) -> bool:
        text = resp.text.lower()
        if any(marker in text for marker in ("per-day", "per day", "daily", "quota", "per-month")):
            return True
        reset = resp.headers.get("x-ratelimit-reset")
        try:  # reset more than 10 minutes away (seconds or milliseconds since epoch)
            value = float(reset) / (1000 if reset and float(reset) > 1e11 else 1)
            import time
            return value - time.time() > 600
        except (TypeError, ValueError):
            return False

    def _parse(self, resp: httpx.Response, messages: List[Message]) -> LLMResponse:
        status = resp.status_code
        body_text = redact_account_ids(resp.text[:500])
        if status == 429:
            # A daily/monthly quota will not come back within this task: disable the key and move on
            # (next key or fallback provider) instead of waiting until the time budget is gone.
            if self._quota_exhausted(resp):
                raise LLMError(f"quota exhausted (429): {body_text}", status, disable_key=True, rotate_key=True)
            too_large = _TOO_LARGE_RE.search(resp.text)
            if too_large and too_large.group(1).upper() == "OTPM":
                # max_tokens alone exceeds the per-minute output limit: it can never pass as is. Cap it for this
                # model from now on and retry at once (an answer is rarely that long anyway).
                self.max_output_cap = max(64, int(int(too_large.group(2)) * 0.9))
                raise LLMError(f"max_tokens above the provider's output limit, capped to {self.max_output_cap}: {body_text}",
                               status, retry_after=0.1)
            if too_large:  # the input alone exceeds the per-minute token limit: waiting will not help
                raise LLMError(f"request larger than the provider's per-minute limit: {body_text}", status, retryable=False)
            wait = self._retry_after(resp)
            if wait is None:
                m = _TRY_AGAIN_RE.search(resp.text)
                wait = parse_duration(m.group(1)) if m else None
            if wait is not None:
                self.tokens_left, self.tokens_reset_at = 0, time.monotonic() + wait  # the window is full until then
            raise LLMError(f"rate limited (429): {body_text}", status, rotate_key=True,
                           retry_after=None if wait is None else round(wait + 0.3, 2))
        if status in (401, 403):
            raise LLMError(f"authentication failed ({status}): {body_text}", status, disable_key=True,
                           rotate_key=True)
        if status == 402:
            raise LLMError(f"quota exhausted (402): {body_text}", status, disable_key=True, rotate_key=True)
        if status >= 500 or status == 408:
            raise LLMError(f"server error ({status}): {body_text}", status, retryable=True,
                           retry_after=self._retry_after(resp))
        if status >= 400:
            raise LLMError(f"request rejected ({status}): {body_text}", status, retryable=False)
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"invalid JSON response: {body_text}", status) from exc
        if isinstance(data, dict) and data.get("error"):
            # Some gateways (OpenRouter) return 200 with an error object, e.g. upstream rate limits.
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            raise LLMError(f"provider error: {str(err)[:400]}", code if isinstance(code, int) else None,
                           rotate_key=code == 429)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"response has no choices: {str(data)[:300]}")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        usage = data.get("usage") or {}
        in_tok, out_tok = usage.get("prompt_tokens"), usage.get("completion_tokens")
        estimated = in_tok is None or out_tok is None
        if estimated:
            in_tok = estimate_messages_tokens(messages)
            out_tok = estimate_tokens(text + (msg.get("reasoning") or ""))
        return LLMResponse(text=text, input_tokens=int(in_tok), output_tokens=int(out_tok),
                           finish_reason=choices[0].get("finish_reason") or "", usage_estimated=estimated)
