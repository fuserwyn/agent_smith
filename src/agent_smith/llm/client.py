"""LLMClient: retries, key rotation, provider fallback and usage accounting."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from agent_smith.config import ModelsConfig, keys_from_env, load_models_config
from agent_smith.llm.base import ChatResult, LLMError, Message, Provider
from agent_smith.llm.key_pool import KeyPool
from agent_smith.llm.openai_compat import OpenAICompatProvider

log = logging.getLogger(__name__)


@dataclass
class Endpoint:
    provider: Provider
    keys: KeyPool
    name: str = ""

    @property
    def label(self) -> str:
        return f"{self.name or self.provider.base_url}:{self.provider.model}"


class LLMClient:
    """Calls the primary endpoint; on persistent failure falls back to the next one.

    Every HTTP attempt counts as a request. ``retries`` = failed attempts before success.
    """

    def __init__(self, endpoints: List[Endpoint], max_attempts: int = 6, temperature: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        if not endpoints:
            raise ValueError("at least one endpoint is required")
        self.endpoints = endpoints
        self.max_attempts = max_attempts
        self.temperature = temperature
        self._sleep = sleep
        self._on_event = on_event or (lambda kind, data: None)
        self.total_requests = 0

    @property
    def primary(self) -> Endpoint:
        return self.endpoints[0]

    def chat(self, messages: List[Message], max_tokens: int, stop: Optional[List[str]] = None,
             deadline: Optional[float] = None, temperature: Optional[float] = None) -> ChatResult:
        """Complete ``messages``. ``deadline`` is a ``time.monotonic()`` instant we must not exceed."""
        retries, requests, errors = 0, 0, []
        for ep_index, endpoint in enumerate(self.endpoints):
            attempts = 0
            while attempts < self.max_attempts:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining < 2:
                    raise LLMError("time budget exhausted before the LLM answered: " + "; ".join(errors[-3:]),
                                   retryable=False)
                try:
                    key, wait = endpoint.keys.acquire()
                except RuntimeError as exc:
                    errors.append(f"{endpoint.label}: {exc}")
                    break
                if wait:
                    if remaining is not None and wait > remaining - 2:
                        errors.append(f"{endpoint.label}: all keys cooling down")
                        break
                    self._sleep(wait)
                attempts += 1
                requests += 1
                self.total_requests += 1
                timeout = 120.0 if remaining is None else max(2.0, min(120.0, remaining - 1))
                t0 = time.monotonic()
                try:
                    resp = endpoint.provider.complete(messages, api_key=key, max_tokens=max_tokens, stop=stop,
                                                      temperature=self.temperature if temperature is None else temperature,
                                                      timeout=timeout)
                except LLMError as exc:
                    retries += 1
                    errors.append(f"{endpoint.label} key {KeyPool.mask(key)}: {exc}")
                    log.warning("LLM attempt failed (%s): %s", endpoint.label, exc)
                    self._on_event("llm_retry", {"endpoint": endpoint.label, "error": str(exc)[:300],
                                                 "status": exc.status})
                    if exc.disable_key:
                        endpoint.keys.disable(key)
                    elif exc.rotate_key:
                        endpoint.keys.cooldown(key, exc.retry_after or 20.0)
                    if not exc.retryable:
                        break
                    if not (exc.rotate_key and len(endpoint.keys) > 1):
                        backoff = exc.retry_after or min(30.0, 1.5 * 2 ** (attempts - 1)) + random.random()
                        if remaining is not None:
                            backoff = min(backoff, max(0.0, remaining - 3))
                        self._sleep(backoff)
                    continue
                elapsed_ms = (time.monotonic() - t0) * 1000
                if resp.usage_estimated:
                    log.warning("provider returned no usage; token counts are estimated")
                return ChatResult(text=resp.text, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                                  request_time_ms=elapsed_ms, retries=retries, requests=requests,
                                  api_url=endpoint.provider.base_url, model_name=endpoint.provider.model,
                                  finish_reason=resp.finish_reason, errors=errors)
            if ep_index + 1 < len(self.endpoints):
                log.warning("falling back from %s to %s", endpoint.label, self.endpoints[ep_index + 1].label)
                self._on_event("llm_fallback", {"from": endpoint.label, "to": self.endpoints[ep_index + 1].label})
        raise LLMError("all LLM attempts failed: " + "; ".join(errors[-5:]), retryable=False)


def build_client(model_name: str, provider_url: str, use_fallbacks: bool = False,
                 models_config: Optional[ModelsConfig] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None) -> LLMClient:
    """Create a client for ``--model-name`` / ``--provider-url`` with keys from the environment."""
    cfg = models_config or load_models_config()
    spec = cfg.provider_for_url(provider_url)
    key_env = spec.key_env if spec else "LLM_API_KEY"
    keys = keys_from_env(key_env) or keys_from_env("LLM_API_KEY")
    if not keys and spec is not None and spec.key_env:
        raise SystemExit(f"error: no API key found. Set {spec.key_env} (or {spec.key_env}_2.., "
                         f"{spec.key_env}S=k1,k2) in the environment or .env file.")
    endpoints = [Endpoint(OpenAICompatProvider(provider_url, model_name, spec.extra_headers if spec else None,
                                               extra_body=spec.extra_body if spec else None),
                          KeyPool(keys), spec.name if spec else "")]
    if use_fallbacks or cfg.fallback_enabled:
        for fb in cfg.fallbacks:
            fspec = cfg.provider_by_name(fb.provider)
            if not fspec or (fspec.base_url.rstrip("/") == provider_url.rstrip("/") and fb.model == model_name):
                continue
            fkeys = keys_from_env(fspec.key_env)
            if fspec.key_env and not fkeys:
                continue
            endpoints.append(Endpoint(OpenAICompatProvider(fspec.base_url, fb.model, fspec.extra_headers,
                                                           extra_body=fspec.extra_body),
                                      KeyPool(fkeys), fspec.name))
    return LLMClient(endpoints, on_event=on_event)
