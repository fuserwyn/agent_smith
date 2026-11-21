"""Claude Code as an engine of the studio chat: the official ``claude`` CLI, on your Claude subscription.

The turn runs ``claude -p … --output-format stream-json`` in the project folder with only
``CLAUDE_CODE_OAUTH_TOKEN`` (from Agent Smith's ``.env``) as credentials — no other key of Agent Smith reaches it.
Its stream is translated into the studio's trace events, so the chat shows each step, tool call and result
like a turn of our own agent. Ask mode = Claude Code's ``plan`` mode (read-only); Agent mode = ``auto``
(Claude Code's own permission classifier decides). A follow-up turn resumes the same Claude Code session.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_smith.config import keys_from_env

PROVIDER = "claude-code"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
MODELS = [  # aliases of the CLI: always the latest model of each family
    ("sonnet", "Claude Sonnet (latest) — the everyday choice"),
    ("opus", "Claude Opus (latest) — hardest tasks"),
    ("haiku", "Claude Haiku (latest) — fast and light on your limits"),
    ("fable", "Claude Fable (latest) — the most capable"),
]
WRITE_TOOLS = {"Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"}  # marks around these: what they change is the turn's
# environment of the CLI: what a shell needs, plus the token. Never the provider keys of Agent Smith.
PASS_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "TMPDIR")


def binary() -> Optional[str]:
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.exists() else None


def token() -> Optional[str]:
    keys = keys_from_env(TOKEN_ENV)
    return keys[0] if keys else None


def unavailable() -> Optional[str]:
    if not binary():
        return "the Claude Code CLI is not installed (https://claude.com/claude-code)"
    if not token():
        return f"no {TOKEN_ENV} in Agent Smith's .env (create one with `claude setup-token`)"
    return None


def choices() -> List[Dict[str, Any]]:
    ok = unavailable() is None
    return [{"provider": PROVIDER, "model": alias, "url": "", "notes": notes, "key_env": TOKEN_ENV, "key_ok": ok,
             "paid": False, "tier": "claude", "limits": "your Claude plan's usage limits", "price": None,
             "inputs": ["text", "image", "file"], "live": None} for alias, notes in MODELS]


def _args(tool: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Claude Code tool input -> the argument names the chat's tool chips show."""
    out = dict(data)
    if "file_path" in data:
        out["filepath"] = data["file_path"]
    if tool in ("Glob", "Grep") and "path" in data:
        out.setdefault("directory", data["path"])
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


class ClaudeCodeTurn:
    """One chat turn through the CLI. ``emit(kind, data)`` writes studio trace events."""

    def __init__(self, root: Path, emit: Callable[[str, Dict[str, Any]], None], cancelled: Callable[[], bool],
                 mark: Optional[Callable[[], str]] = None, changed: Optional[Callable[[str, str], List[str]]] = None):
        self.root, self.emit, self.cancelled = Path(root), emit, cancelled
        self.mark, self.changed = mark, changed  # project marks around writing tools (Checkpoints.tree / changed_between)
        self.touched: List[str] = []
        self.steps = 0
        self.input_tokens = self.output_tokens = 0
        self.cost_usd: Optional[float] = None
        self.session_id: Optional[str] = None
        self.answer = ""
        self.error: Optional[str] = None

    def command(self, prompt: str, model: str, mode: str, resume: Optional[str]) -> List[str]:
        cmd = [binary() or "claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", model,
               "--permission-mode", "plan" if mode == "ask" else "auto"]
        if resume:
            cmd += ["--resume", resume]
        return cmd

    def env(self) -> Dict[str, str]:
        env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
        env[TOKEN_ENV] = token() or ""
        return env

    def run(self, prompt: str, model: str, mode: str, resume: Optional[str] = None) -> bool:
        proc = subprocess.Popen(self.command(prompt, model, mode, resume), cwd=self.root, env=self.env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, start_new_session=True)
        done = threading.Event()

        def watch() -> None:  # the Stop button: end the whole process group
            while not done.wait(0.3):
                if self.cancelled():
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except OSError:
                        pass
                    return
        threading.Thread(target=watch, daemon=True).start()
        last_mark = self.mark() if self.mark else ""
        names: Dict[str, str] = {}
        result: Optional[Dict[str, Any]] = None
        last_id: Optional[str] = None
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "system" and ev.get("subtype") == "init":
                    self.session_id = ev.get("session_id") or self.session_id
                    self.emit("claude_code", {"session": self.session_id, "model": ev.get("model"),
                                              "permission_mode": ev.get("permissionMode")})
                elif kind == "assistant":
                    msg = ev.get("message") or {}
                    text = "\n".join(c.get("text", "") for c in msg.get("content") or [] if c.get("type") == "text").strip()
                    if msg.get("id") is None or msg.get("id") != last_id:  # one API reply can arrive as several events
                        last_id = msg.get("id")
                        self.steps += 1
                        usage = msg.get("usage") or {}
                        tin = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                        self.emit("llm_request", {"step": self.steps})
                        self.emit("llm_response", {"step": self.steps, "text": f"Thought: {text}" if text else "",
                                                   "input_tokens": tin, "output_tokens": int(usage.get("output_tokens") or 0),
                                                   "cached_tokens": int(usage.get("cache_read_input_tokens") or 0)})
                    elif text:
                        self.emit("llm_note", {"step": self.steps, "text": text})
                    for c in msg.get("content") or []:
                        if c.get("type") == "tool_use":
                            names[c.get("id", "")] = c.get("name", "?")
                            self.emit("tool_call", {"step": self.steps, "name": c.get("name", "?"),
                                                    "args": _args(c.get("name", ""), c.get("input") or {})})
                elif kind == "user":
                    for c in (ev.get("message") or {}).get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            tid = c.get("tool_use_id", "")
                            self.emit("tool_result", {"name": names.get(tid, "?"), "result": _text(c.get("content"))[:4000],
                                                      "is_error": bool(c.get("is_error"))})
                            # the stream can lag behind the tools, so the marks are taken between results: what
                            # changed since the previous result, when a writing tool ran, is the agent's doing
                            if self.mark and self.changed:
                                now = self.mark()
                                if names.get(tid) in WRITE_TOOLS and last_mark:
                                    for p in self.changed(last_mark, now):
                                        if p not in self.touched:
                                            self.touched.append(p)
                                last_mark = now
                elif kind == "result":
                    result = ev
            proc.wait()
        finally:
            done.set()
        err = proc.stderr.read() if proc.stderr else ""
        if result:
            usage = result.get("usage") or {}
            self.input_tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            self.output_tokens = int(usage.get("output_tokens") or 0)
            self.cost_usd = result.get("total_cost_usd")
            self.session_id = result.get("session_id") or self.session_id
            self.answer = str(result.get("result") or "")
            if result.get("is_error") or result.get("subtype") != "success":
                self.error = self.answer or str(result.get("subtype"))
            self.emit("final_answer", {"answer": self.answer})
            return not self.error
        if self.cancelled():
            self.error = "stopped by the user"
        else:
            self.error = (err.strip().splitlines() or [f"claude exited with code {proc.returncode}"])[-1][:500]
        return False
