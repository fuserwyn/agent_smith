"""OpenAI-compatible chat-completions provider (OpenRouter, Groq, Gemini, Mistral, Ollama...)."""

from __future__ import annotations

from typing import Dict, List, Optional

import httpx

from agent_smith.llm.base import LLMError, LLMResponse, Message, estimate_messages_tokens, estimate_tokens


class OpenAICompatProvider:
    def __init__(self, base_url: str, model: str, extra_headers: Optional[Dict[str, str]] = None,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_headers = extra_headers or {}
        self._client = httpx.Client(transport=transport)

    def complete(self, messages: List[Message], *, api_key: Optional[str], max_tokens: int,
                 stop: Optional[List[str]], temperature: float, timeout: float) -> LLMResponse:
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature}
        if stop:
            payload["stop"] = stop[:4]  # many providers accept at most 4 stop sequences
        try:
            resp = self._client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers,
                                     timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)))
        except httpx.TimeoutException as exc:
            raise LLMError(f"request timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"network error: {exc}", retryable=True) from exc
        return self._parse(resp, messages)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> Optional[float]:
        value = resp.headers.get("retry-after")
        try:
            return float(value) if value else None
        except ValueError:
            return None

    def _parse(self, resp: httpx.Response, messages: List[Message]) -> LLMResponse:
        status = resp.status_code
        body_text = resp.text[:500]
        if status == 429:
            raise LLMError(f"rate limited (429): {body_text}", status, rotate_key=True,
                           retry_after=self._retry_after(resp))
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
