"""Cumulative budget tracking (iterations, input/output tokens, wall-clock)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent_smith.models import Limits


@dataclass
class Budget:
    limits: Limits
    started: float = field(default_factory=time.monotonic)
    safety_seconds: float = 8.0
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    @property
    def deadline(self) -> float:
        return self.started + self.limits.timeout_seconds - self.safety_seconds

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    def input_left(self) -> int:
        return self.limits.max_input_tokens - self.input_tokens

    def output_left(self) -> int:
        return self.limits.max_output_tokens - self.output_tokens

    def iterations_left(self) -> int:
        return self.limits.max_iterations - self.iterations

    def can_start_step(self, estimated_input: int, min_output: int = 64) -> "tuple[bool, str]":
        """Would a new LLM call stay within every limit?"""
        if self.iterations_left() <= 0:
            return False, f"iteration limit reached ({self.limits.max_iterations})"
        if self.time_left() < 5:
            return False, f"time limit almost reached ({self.elapsed():.0f}s of {self.limits.timeout_seconds:.0f}s)"
        if estimated_input > self.input_left():
            return False, (f"input-token budget exhausted (used {self.input_tokens}, next call needs "
                           f"~{estimated_input}, limit {self.limits.max_input_tokens})")
        if self.output_left() < min_output:
            return False, f"output-token budget exhausted ({self.output_tokens}/{self.limits.max_output_tokens})"
        return True, ""

    def record(self, input_tokens: int, output_tokens: int, requests: int) -> None:
        self.iterations += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.requests += requests

    def snapshot(self) -> dict:
        return {
            "iterations": self.iterations, "max_iterations": self.limits.max_iterations,
            "input_tokens": self.input_tokens, "max_input_tokens": self.limits.max_input_tokens,
            "output_tokens": self.output_tokens, "max_output_tokens": self.limits.max_output_tokens,
            "elapsed": round(self.elapsed(), 2), "timeout": self.limits.timeout_seconds,
        }
