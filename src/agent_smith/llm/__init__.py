"""LLM provider abstraction: OpenAI-compatible HTTP providers, key rotation, retries, fallback."""

from agent_smith.llm.base import ChatResult, LLMError, LLMResponse, Message, estimate_messages_tokens, estimate_tokens
from agent_smith.llm.client import Endpoint, LLMClient, build_client
from agent_smith.llm.key_pool import KeyPool
from agent_smith.llm.openai_compat import OpenAICompatProvider

__all__ = ["ChatResult", "Endpoint", "KeyPool", "LLMClient", "LLMError", "LLMResponse", "Message",
           "OpenAICompatProvider", "build_client", "estimate_messages_tokens", "estimate_tokens"]
