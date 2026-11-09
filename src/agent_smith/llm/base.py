"""Provider-agnostic LLM types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

Message = Dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}


@dataclass
class LLMResponse:
    """One successful completion."""

    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str = ""
    usage_estimated: bool = False


@dataclass
class ChatResult:
    """A completion plus the bookkeeping the agent needs for StepMetrics."""

    text: str
    input_tokens: int
    output_tokens: int
    request_time_ms: float
    retries: int
    requests: int
    api_url: str
    model_name: str
    finish_reason: str = ""
    errors: List[str] = field(default_factory=list)


class LLMError(Exception):
    """Base error. ``retryable`` tells the client whether trying again may help."""

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = True,
                 rotate_key: bool = False, disable_key: bool = False, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.rotate_key = rotate_key
        self.disable_key = disable_key
        self.retry_after = retry_after


class Provider(Protocol):
    """Anything that can complete a chat. Implementations: OpenAI-compatible HTTP, scripted (tests)."""

    base_url: str
    model: str

    def complete(self, messages: List[Message], *, api_key: Optional[str], max_tokens: int,
                 stop: Optional[List[str]], temperature: float, timeout: float) -> LLMResponse:
        ...


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (~3 chars/token for code-heavy text)."""
    return len(text) // 3 + 1


def estimate_messages_tokens(messages: List[Message]) -> int:
    return sum(estimate_tokens(m.get("content", "")) + 4 for m in messages) + 3
