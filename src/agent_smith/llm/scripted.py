"""Scripted provider — TESTS AND DEMOS ONLY.

Replays predefined responses so the full loop (extraction, sandbox, MCP,
metrics) can be exercised offline. It must never be used to produce benchmark
data: its api_url is ``scripted://`` so such runs are trivially identifiable.
"""

from __future__ import annotations

from typing import List, Optional

from agent_smith.llm.base import LLMError, LLMResponse, Message, estimate_messages_tokens, estimate_tokens


class ScriptedProvider:
    base_url = "scripted://local"

    def __init__(self, responses: List[str], model: str = "scripted-test-model"):
        self.responses = list(responses)
        self.model = model
        self.calls: List[List[Message]] = []

    def complete(self, messages: List[Message], *, api_key: Optional[str], max_tokens: int,
                 stop: Optional[List[str]], temperature: float, timeout: float) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if not self.responses:
            raise LLMError("scripted provider has no more responses", retryable=False)
        text = self.responses.pop(0)
        for s in stop or []:
            if s in text:
                text = text.split(s, 1)[0]
        return LLMResponse(text=text, input_tokens=estimate_messages_tokens(messages),
                           output_tokens=min(max_tokens, estimate_tokens(text)), finish_reason="stop")
