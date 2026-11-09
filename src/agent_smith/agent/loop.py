"""The agent loop: Thought -> Code -> Observation, until final_answer().

    while budget allows:
        response   = LLM(messages)                       # Thought + code
        extraction = extract_code(response)               # any supported format -> Python
        result     = sandbox.execute(extraction.code)     # tools are proxied to MCP
        if result.is_final: break                         # final_answer() was called
        messages  += [assistant(response), user("Observation: ...")]

Every step is recorded as a StepMetrics; the run produces a SolutionOutput.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from agent_smith.agent.budget import Budget
from agent_smith.events import NULL_SINK, EventSink
from agent_smith.extraction import END_CODE, NO_CODE_FEEDBACK, STOP_SEQUENCES, extract_code
from agent_smith.llm import LLMClient, LLMError, Message, estimate_messages_tokens
from agent_smith.models import Limits, SolutionOutput, StepMetrics
from agent_smith.sandbox.host import ExecutionResult, Sandbox

log = logging.getLogger(__name__)


@dataclass
class AgentSettings:
    max_output_tokens_per_call: int = 1024
    observation_max_chars: int = 6000     # observation of the latest steps
    old_observation_chars: int = 800      # observations older than `keep_recent_steps`
    keep_recent_steps: int = 3
    show_budget: bool = False             # append a budget line to each observation
    safety_seconds: float = 8.0
    input_estimate_margin: float = 1.1


def truncate_observation(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.4), int(limit * 0.6)
    return (text[:head] + f"\n[... observation truncated: {len(text)} chars total, showing first {head} and "
            f"last {tail}. Print less or narrow your query ...]\n" + text[-tail:])


class CodeAgent:
    def __init__(self, llm: LLMClient, sandbox: Sandbox, system_prompt: str, limits: Limits, benchmark: str,
                 task_id: str, settings: Optional[AgentSettings] = None, events: EventSink = NULL_SINK,
                 fallback_answer: Optional[Callable[[], Optional[str]]] = None, started: Optional[float] = None):
        self.llm = llm
        self.sandbox = sandbox
        self.system_prompt = system_prompt
        self.limits = limits
        self.benchmark = benchmark
        self.task_id = task_id
        self.settings = settings or AgentSettings()
        self.events = events
        self.fallback_answer = fallback_answer
        self.budget = Budget(limits, started=started or time.monotonic(), safety_seconds=self.settings.safety_seconds)
        self.steps: List[StepMetrics] = []
        self.history: List[tuple] = []  # (assistant_text, observation_text)
        self.interrupted = False
        self.known_tools = [t.name for t in sandbox.mcp.tools] if sandbox.mcp else []

    # ------------------------------------------------------------------ #
    def _messages(self, task_prompt: str, compact_all: bool = False) -> List[Message]:
        msgs: List[Message] = [{"role": "system", "content": self.system_prompt},
                               {"role": "user", "content": task_prompt}]
        n = len(self.history)
        for i, (assistant, observation) in enumerate(self.history):
            recent = (n - i) <= self.settings.keep_recent_steps and not (compact_all and n - i > 1)
            limit = self.settings.observation_max_chars if recent else self.settings.old_observation_chars
            msgs.append({"role": "assistant", "content": assistant})
            msgs.append({"role": "user", "content": "Observation:\n" + truncate_observation(observation, limit)})
        return msgs

    def _compacted(self, messages: List[Message]) -> List[int]:
        """Steps whose observation reaches the LLM shortened in this request."""
        sent = [m["content"] for m in messages[2:] if m["role"] == "user"]
        return [i + 1 for i, ((_, obs), text) in enumerate(zip(self.history, sent))
                if text != "Observation:\n" + obs]

    def _estimate(self, messages: List[Message]) -> int:
        return int(estimate_messages_tokens(messages) * self.settings.input_estimate_margin)

    def _observation(self, notes: List[str], result: Optional[ExecutionResult], code: str) -> str:
        parts = [f"[NOTE] {n}" for n in notes]
        if result is not None:
            parts.append(result.render())
        if self.steps and code and self.steps[-1].sandbox_input == code:
            parts.append("[WARNING] You executed exactly the same code as in the previous step. Change approach.")
        if self.settings.show_budget:
            b = self.budget
            parts.append(f"[budget] step {b.iterations}/{b.limits.max_iterations}, "
                         f"output tokens left {b.output_left()}, time left {max(0, b.time_left()):.0f}s")
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    def run(self, task_prompt: str) -> SolutionOutput:
        ev = self.events
        ev("run_start", {"benchmark": self.benchmark, "task_id": self.task_id, "limits": self.limits.model_dump(),
                         "system_prompt": self.system_prompt, "task_prompt": task_prompt,
                         "model": self.llm.primary.provider.model, "api_url": self.llm.primary.provider.base_url,
                         "tools": self.known_tools})
        solution: Optional[str] = None
        success = False
        error: Optional[str] = None
        try:
            while True:
                messages = self._messages(task_prompt)
                estimate = self._estimate(messages)
                ok, reason = self.budget.can_start_step(estimate)
                if not ok and "input-token" in reason:
                    messages = self._messages(task_prompt, compact_all=True)
                    estimate = self._estimate(messages)
                    ok, reason = self.budget.can_start_step(estimate)
                if not ok:
                    error = f"stopped before final_answer: {reason}"
                    ev("budget_stop", {"reason": reason, **self.budget.snapshot()})
                    break
                step_no = self.budget.iterations + 1
                max_tokens = max(1, min(self.settings.max_output_tokens_per_call, self.budget.output_left()))
                ev("llm_request", {"step": step_no, "messages": len(messages), "estimated_input_tokens": estimate,
                                   "max_tokens": max_tokens, "delivered": messages[-1]["content"],
                                   "compacted_steps": self._compacted(messages)})
                try:
                    chat = self.llm.chat(messages, max_tokens=max_tokens, stop=STOP_SEQUENCES,
                                         deadline=self.budget.deadline)
                except LLMError as exc:
                    error = f"LLM failure: {exc}"
                    ev("llm_error", {"step": step_no, "error": str(exc)[:500]})
                    break
                self.budget.record(chat.input_tokens, chat.output_tokens, chat.requests)
                ev("llm_response", {"step": step_no, "text": chat.text, "input_tokens": chat.input_tokens,
                                    "output_tokens": chat.output_tokens, "request_time_ms": chat.request_time_ms,
                                    "retries": chat.retries, "finish_reason": chat.finish_reason})

                extraction = extract_code(chat.text, self.known_tools)
                ev("extraction", {"step": step_no, "format": extraction.format, "code": extraction.code or "",
                                  "notes": extraction.notes})
                result: Optional[ExecutionResult] = None
                code = extraction.code or ""
                if extraction.found:
                    ev("sandbox_start", {"step": step_no, "code": code})
                    try:
                        result = self.sandbox.execute(code)
                    except SystemExit as exc:
                        error = f"sandboxed code raised SystemExit({exc.code!r}); agent stopped"
                        self._record_step(step_no, chat, code, error)
                        ev("sandbox_end", {"step": step_no, "status": "system_exit", "output": error})
                        break
                    observation = self._observation(extraction.notes, result, code)
                    ev("sandbox_end", {"step": step_no, "status": result.status, "output": observation,
                                       "duration_s": round(result.duration_s, 3), "is_final": result.is_final,
                                       "stdout": result.stdout, "value": result.value, "error": result.error,
                                       "trace": result.trace})
                else:
                    observation = "\n".join([f"[NOTE] {n}" for n in extraction.notes] + [NO_CODE_FEEDBACK])

                self._record_step(step_no, chat, code, observation if extraction.found else "")
                ev("budget", self.budget.snapshot())

                if result is not None and result.is_final:
                    solution = result.final_answer or ""
                    success = bool(solution.strip())
                    if not success:
                        error = "final_answer was called with an empty answer"
                    ev("final_answer", {"step": step_no, "answer": solution})
                    break
                assistant = chat.text.rstrip()
                if extraction.format.startswith("python_block") and END_CODE not in assistant:
                    assistant += END_CODE
                self.history.append((assistant, observation))
                ev("observation", {"step": step_no, "text": observation})
        except KeyboardInterrupt:
            self.interrupted = True
            error = "interrupted (KeyboardInterrupt)"
            ev("interrupted", {})

        if solution is None:
            solution = ""
            if self.fallback_answer is not None:
                try:
                    solution = self.fallback_answer() or ""
                except Exception as exc:  # noqa: BLE001 - best effort
                    log.warning("fallback answer failed: %s", exc)
                if solution:
                    error = (error or "no final_answer") + "; submitted best available candidate"
                    ev("fallback_answer", {"answer": solution})

        output = SolutionOutput(
            task_id=str(self.task_id), benchmark=self.benchmark, success=success, solution=solution,
            iterations=len(self.steps), total_requests=self.llm.total_requests,
            total_input_tokens=sum(s.input_tokens for s in self.steps),
            total_output_tokens=sum(s.output_tokens for s in self.steps),
            total_time_seconds=round(self.budget.elapsed(), 3), steps=self.steps,
            system_prompt=self.system_prompt, error=error,
        )
        ev("run_end", {"success": success, "error": error, "solution": solution,
                       "total_time_seconds": output.total_time_seconds, **self.budget.snapshot()})
        return output

    def _record_step(self, step_no: int, chat, code: str, sandbox_output: str) -> None:
        self.steps.append(StepMetrics(
            step=step_no, input_tokens=chat.input_tokens, output_tokens=chat.output_tokens,
            request_time_ms=round(chat.request_time_ms, 2), api_url=chat.api_url, model_name=chat.model_name,
            llm_output=chat.text, sandbox_input=code, sandbox_output=sandbox_output, retries=chat.retries,
        ))
