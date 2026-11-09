"""Host side of the sandbox.

:class:`Sandbox` owns two things:

* a **worker process** (``worker.py``) where untrusted code runs, and
* an optional **MCP client**; tool calls made by sandboxed code are proxied
  through the JSON protocol to this process, which forwards them to the MCP
  server. ("The sandbox wraps the MCP client, not the other way around.")

The host enforces the hard limits the worker cannot enforce on itself:
it kills the worker when the *sandbox* time (excluding tool calls) exceeds the
timeout + grace, or when its resident memory exceeds the limit, then restarts
it and reports the reset explicitly.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_smith.models import SandboxConfig

WORKER_PATH = Path(__file__).with_name("worker.py")
HARD_KILL_GRACE_S = 2.0


@dataclass
class ToolCallRecord:
    name: str
    args: Dict[str, Any]
    result: str
    is_error: bool
    duration_ms: float


@dataclass
class ExecutionResult:
    """Everything the agent loop needs to build an observation."""

    status: str  # ok | error | syntax_error | security_error | timeout | memory | final_answer | crash | ...
    stdout: str = ""
    value: Optional[str] = None
    error: Optional[str] = None
    final_answer: Optional[str] = None
    is_final: bool = False
    truncated: bool = False
    state_reset: bool = False
    duration_s: float = 0.0
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    trace: Optional[Dict[str, Any]] = None  # line-by-line execution path (when tracing is enabled)

    def render(self) -> str:
        """Human/LLM readable observation text (notes are explicit, never silent)."""
        parts: List[str] = []
        if self.stdout:
            parts.append(self.stdout.rstrip("\n"))
        if self.value is not None:
            parts.append(f"Out: {self.value}")
        if self.error:
            parts.append(self.error)
        notes = []
        if self.truncated:
            notes.append("[NOTE] Printed output exceeded the sandbox limit and was truncated.")
        if self.status == "timeout":
            notes.append("[NOTE] Execution hit the timeout; the output above is PARTIAL.")
        if self.state_reset:
            notes.append("[NOTE] The sandbox process was restarted: all variables, imports and "
                         "definitions from previous steps are LOST. Re-define what you need.")
        if self.is_final:
            notes.append("[final_answer received]")
        if not parts and not notes:
            parts.append("(code executed successfully, no output — use print() to see values)")
        return "\n".join(parts + notes)


class SandboxError(RuntimeError):
    pass


class _LineReader:
    """Non-blocking line reader over a pipe with deadlines."""

    def __init__(self, stream):
        self.fd = stream.fileno()
        self.buf = b""
        self.sel = selectors.DefaultSelector()
        self.sel.register(self.fd, selectors.EVENT_READ)

    def readline(self, timeout: Optional[float]) -> Optional[str]:
        """Return a line, ``None`` on timeout; raises EOFError when the pipe closes."""
        end = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while b"\n" not in self.buf:
            remaining = None if end is None else end - time.monotonic()
            if remaining is not None and remaining <= 0:
                return None
            if not self.sel.select(remaining):
                return None
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", "replace")


def _rss_kb(pid: int) -> int:
    """Resident memory of ``pid`` in KiB (Linux /proc, otherwise ``ps``)."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return int(out.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


ToolHandler = Callable[[str, Dict[str, Any]], "tuple[str, bool]"]


class Sandbox:
    """A persistent, restricted Python execution environment."""

    def __init__(self, config: Optional[SandboxConfig] = None, mcp_client=None,
                 event_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 trace_execution: bool = False):
        self.config = config or SandboxConfig()
        self.trace_execution = trace_execution
        self.mcp = mcp_client
        self.event_hook = event_hook or (lambda kind, data: None)
        self.proc: Optional[subprocess.Popen] = None
        self.reader: Optional[_LineReader] = None
        self.baseline_rss_kb = 0
        self._stderr_tail: List[str] = []
        self._scratch = tempfile.mkdtemp(prefix="agent-smith-sbx-")
        self.tool_log: List[ToolCallRecord] = []

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def tool_specs(self) -> List[Dict[str, Any]]:
        return [t.to_worker() for t in self.mcp.tools] if self.mcp else []

    def extras(self) -> List[str]:
        extras = []
        if self.mcp and self.mcp.has_resources:
            extras.append("resources")
        if self.mcp and self.mcp.has_prompts:
            extras.append("prompts")
        return extras

    def start(self) -> "Sandbox":
        cwd = next((d for d in self.config.allowed_directories if os.path.isdir(d)), self._scratch)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0"}
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-S", str(WORKER_PATH)] if self._can_skip_site() else
            [sys.executable, "-I", str(WORKER_PATH)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=env, start_new_session=True,  # Ctrl+C reaches the host only
        )
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        self.reader = _LineReader(self.proc.stdout)
        self._send({"type": "init", "config": self.config.model_dump(), "tools": self.tool_specs(),
                    "extras": self.extras(), "trace": self.trace_execution})
        line = self.reader.readline(30)
        if line is None:
            self._kill()
            raise SandboxError("sandbox worker did not start: " + "".join(self._stderr_tail[-20:]))
        msg = json.loads(line)
        self.baseline_rss_kb = int(msg.get("rss_kb", 0))
        return self

    @staticmethod
    def _can_skip_site() -> bool:
        # The worker only needs the stdlib; -S keeps third-party site-packages out of reach.
        return True

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        for raw in iter(proc.stderr.readline, b""):
            self._stderr_tail.append(raw.decode("utf-8", "replace"))
            del self._stderr_tail[:-200]

    def _send(self, msg: Dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.proc.kill()
            self.proc.wait(timeout=5)

    def restart(self) -> None:
        self._kill()
        self.start()

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._send({"type": "shutdown"})
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001 - fall back to kill
                pass
        self._kill()
        self.proc = None

    def __enter__(self) -> "Sandbox":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # tool proxy
    # ------------------------------------------------------------------ #
    def _dispatch_tool(self, name: str, args: Dict[str, Any]) -> "tuple[str, bool, Dict[str, Any]]":
        """Forward a tool call to MCP. Returns (text, is_error, hop details for the trace)."""
        if self.mcp is None:
            return f"no MCP server connected (tool '{name}')", True, {}
        try:
            if name == "__list_resources":
                return self.mcp.list_resources(), False, {"request": {"method": "resources/list"}}
            if name == "__read_resource":
                return self.mcp.read_resource(args["uri"]), False, {"request": {"method": "resources/read",
                                                                                "params": {"uri": args["uri"]}}}
            if name == "__list_prompts":
                return self.mcp.list_prompts(), False, {"request": {"method": "prompts/list"}}
            if name == "__get_prompt":
                return self.mcp.get_prompt(args["name"], args.get("arguments")), False, {
                    "request": {"method": "prompts/get", "params": args}}
            outcome = self.mcp.call_tool(name, args)
            return outcome.text, outcome.is_error, {"request": outcome.request, "transport": outcome.transport,
                                                     "rtt_ms": outcome.rtt_ms, "server": outcome.meta}
        except Exception as exc:  # noqa: BLE001 - surfaced as ToolError in the sandbox
            return f"{type(exc).__name__}: {exc}", True, {}

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    def execute(self, code: str) -> ExecutionResult:
        """Run ``code`` in the persistent namespace.

        ``SystemExit`` and ``KeyboardInterrupt`` raised by the code are re-raised
        here so they reach the agent loop (they are never silently swallowed).
        """
        if self.proc is None or self.proc.poll() is not None:
            self.start()
        started = time.monotonic()
        budget = float(self.config.max_execution_time_seconds) + HARD_KILL_GRACE_S
        tool_time = 0.0
        out_chunks: List[str] = []
        calls: List[ToolCallRecord] = []
        mem_limit_kb = self.baseline_rss_kb + self.config.max_memory_mb * 1024
        mem_exceeded = threading.Event()
        in_tool = threading.Event()
        stop_watch = threading.Event()

        def memory_watchdog() -> None:
            pid = self.proc.pid
            while not stop_watch.wait(0.1):
                if not in_tool.is_set() and _rss_kb(pid) > mem_limit_kb:
                    mem_exceeded.set()
                    self._kill()
                    return

        watchdog = threading.Thread(target=memory_watchdog, daemon=True)
        self._send({"type": "exec", "code": code})
        watchdog.start()
        try:
            while True:
                sandbox_elapsed = time.monotonic() - started - tool_time
                try:
                    line = self.reader.readline(budget - sandbox_elapsed)
                except EOFError:
                    return self._crashed(out_chunks, calls, started, mem_exceeded.is_set())
                if line is None:  # hard timeout: the worker ignored SIGALRM (e.g. stuck in C code)
                    self._kill()
                    res = ExecutionResult(
                        status="timeout", stdout="".join(out_chunks), tool_calls=calls, state_reset=True,
                        error=f"TimeoutError: execution exceeded {self.config.max_execution_time_seconds}s "
                              f"and the sandbox process had to be killed",
                        duration_s=time.monotonic() - started)
                    self.start()
                    return res
                msg = json.loads(line)
                kind = msg.get("type")
                if kind == "out":
                    out_chunks.append(msg.get("data", ""))
                elif kind == "tool_call":
                    t0 = time.monotonic()
                    in_tool.set()
                    name, args = msg.get("name", ""), msg.get("args", {}) or {}
                    self.event_hook("tool_call", {"name": name.lstrip("_"), "args": args, "line": msg.get("line"),
                                                  "written": msg.get("written", "")})
                    result, is_error, hops = self._dispatch_tool(name, args)
                    in_tool.clear()
                    dt = time.monotonic() - t0
                    tool_time += dt
                    rec = ToolCallRecord(name.lstrip("_"), args, result, is_error, dt * 1000)
                    calls.append(rec)
                    self.tool_log.append(rec)
                    self.event_hook("tool_result", {"name": rec.name, "result": result, "is_error": is_error,
                                                    "duration_ms": rec.duration_ms, "line": msg.get("line"),
                                                    "args": args, "written": msg.get("written", ""), **hops})
                    reply = {"type": "tool_result", "ok": not is_error}
                    reply["result" if not is_error else "error"] = result
                    self._send(reply)
                elif kind == "done":
                    # Allocations faster than the watchdog period are caught here: the
                    # worker reports its (peak) RSS with every result.
                    if int(msg.get("rss_kb", 0)) > mem_limit_kb and msg.get("status") != "memory":
                        self._kill()
                        return self._crashed(out_chunks, calls, started, memory=True)
                    return self._finish(msg, out_chunks, calls, started)
        finally:
            stop_watch.set()

    def _finish(self, msg: Dict[str, Any], out_chunks, calls, started) -> ExecutionResult:
        status = msg.get("status", "ok")
        if status == "system_exit":
            raise SystemExit(msg.get("exit_code"))
        if status == "keyboard_interrupt":
            raise KeyboardInterrupt("raised by sandboxed code")
        final = msg.get("final_answer")
        return ExecutionResult(
            status=status, stdout="".join(out_chunks), value=msg.get("value"), error=msg.get("error"),
            final_answer=final, is_final=final is not None, truncated=bool(msg.get("truncated")),
            duration_s=time.monotonic() - started, tool_calls=calls, trace=msg.get("trace"),
        )

    def _crashed(self, out_chunks, calls, started, memory: bool) -> ExecutionResult:
        if self.proc:
            self.proc.wait(timeout=5)
        if memory:
            status, error = "memory", (f"MemoryError: the code exceeded the memory limit of "
                                       f"{self.config.max_memory_mb} MB and was terminated")
        else:
            code = self.proc.returncode if self.proc else None
            tail = "".join(self._stderr_tail[-10:]).strip()
            status, error = "crash", f"SandboxCrash: the sandbox process died (exit code {code}). {tail}"
        res = ExecutionResult(status=status, stdout="".join(out_chunks), error=error, tool_calls=calls,
                              state_reset=True, duration_s=time.monotonic() - started)
        self.start()
        return res
