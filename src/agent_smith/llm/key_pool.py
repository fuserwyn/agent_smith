"""Round-robin pool of API keys with cooldowns (rate limits) and disabling (bad/exhausted keys)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class _KeyState:
    key: str
    cooldown_until: float = 0.0
    disabled: bool = False
    uses: int = 0
    failures: int = 0


class KeyPool:
    """Rotates keys; a pool without keys yields ``None`` (keyless/self-hosted providers)."""

    def __init__(self, keys: List[str]):
        self._keys = [_KeyState(k) for k in keys]
        self._idx = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keyless(self) -> bool:
        return not self._keys

    def available(self) -> int:
        return sum(1 for k in self._keys if not k.disabled)

    def acquire(self) -> "tuple[Optional[str], float]":
        """Return ``(key, wait_seconds)``: the next usable key and how long to wait before using it."""
        if self.keyless:
            return None, 0.0
        with self._lock:
            live = [k for k in self._keys if not k.disabled]
            if not live:
                raise RuntimeError("all API keys for this provider are disabled (invalid or out of quota)")
            now = time.monotonic()
            n = len(self._keys)
            for step in range(n):
                state = self._keys[(self._idx + step) % n]
                if not state.disabled and state.cooldown_until <= now:
                    self._idx = (self._idx + step + 1) % n
                    state.uses += 1
                    return state.key, 0.0
            soonest = min(live, key=lambda k: k.cooldown_until)
            soonest.uses += 1
            return soonest.key, max(0.0, soonest.cooldown_until - now)

    def cooldown(self, key: Optional[str], seconds: float) -> None:
        for state in self._keys:
            if state.key == key:
                state.cooldown_until = time.monotonic() + seconds
                state.failures += 1

    def disable(self, key: Optional[str]) -> None:
        for state in self._keys:
            if state.key == key:
                state.disabled = True
                state.failures += 1

    @staticmethod
    def mask(key: Optional[str]) -> str:
        return "<none>" if not key else f"...{key[-4:]}"
