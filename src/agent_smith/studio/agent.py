"""The agent in the studio chat: one turn = one run of the Agent Smith CodeAgent on the open project.

* Ask mode: only read tools (search, read, list) — the agent cannot change anything.
* Agent mode: all tools (edit_file, run_command, …); a checkpoint of the project is taken first, so the
  user can review every changed file and undo one file or the whole turn.
* The tools reach the project the same way as the terminal: ``docker exec`` into the project's
  container (default), or directly on disk with ``--shell local``.
* Every event of the run is streamed to the browser and written to a trace (``events.jsonl``) that the
  "Inside the agent" view replays.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_smith.agent import AgentSettings, CodeAgent
from agent_smith.agent.prompts import FORMAT_RULES
from agent_smith.extraction import END_CODE
from agent_smith.codelog import CodeLog, code_root
from agent_smith.config import PROJECT_ROOT, load_env_file, load_models_config, load_sandbox_config
from agent_smith.events import EventSink
from agent_smith.llm import build_client
from agent_smith.mcp_client import MCPClient
from agent_smith.models import Limits
from agent_smith.runner import default_server, server_env
from agent_smith.sandbox import Sandbox
from agent_smith.sandbox.manual import build_manual
from agent_smith.studio.apps import TOOLS as APP_TOOLS, AppManager
from agent_smith.studio.checkpoints import Checkpoints
from agent_smith.studio.filetools import TOOLS as FILE_TOOLS, FileTools, specs as file_specs
from agent_smith.studio.media import Media
from agent_smith.studio.scenarios import TOOL as SCENARIO_TOOL, Scenarios
from agent_smith.studio.terminal import TerminalManager
from agent_smith.studio.workspace import Workspace, WorkspaceError

READ_ONLY_TOOLS = frozenset({"read_file", "list_files", "search_code", "search_function_or_class_definition_in_code",
                             "find_references"})
# 30 min: a turn may wait for a whole Agent Smith benchmark run (run_agent_smith, capped at 25 min)
# output budget of a whole turn (not the provider's limit): 30 steps of up to 2000 tokens need room
LIMITS = Limits(max_iterations=30, max_input_tokens=400_000, max_output_tokens=40_000, timeout_seconds=1800)
# Stop sequences (providers accept 4): an empty ```python``` block is never useful — some small models repeat
# it until the output cap after their first block; it replaces </function_calls>, a format these models don't use.
STOP = [END_CODE, "\nObservation:", "</tool_call>", "```python\n```"]
SETTINGS = AgentSettings(max_output_tokens_per_call=2000, observation_max_chars=7000, old_observation_chars=900,
                         keep_recent_steps=4, show_budget=True, safety_seconds=20.0, stop_sequences=STOP)
HISTORY_TURNS = 6  # earlier exchanges given to the model as context


def studio_system_prompt(config, mcp, root: str, mode: str) -> str:
    names = set(mcp.tool_names()) if mcp else set()
    extra = ""
    if "run_agent_smith" in names:
        extra += ("\nWhen the user asks to run / try / test an MBPP or SWE-bench task (Agent Smith's benchmarks), call "
                  "run_agent_smith(...) in your FIRST step — do not explore the project, do not use run_command or pytest.")
    if "run_app" in names:
        extra += ("\nWhen the user asks for a server / an app to run: write it in agent_code/apps/<name>/ with a Dockerfile, "
                  "then run_app(...), check it with call_app(...), and give the URL.")
    rules = ("You are in ASK mode: you can only read the project (no edits, no commands). Explain, point to files "
             "and lines, and propose changes as code blocks the user can apply."
             if mode == "ask" else
             "You are in AGENT mode: make the requested change directly with the tools (edit_file, run_command…). "
             "Make focused edits, keep the project's style, and verify your change when possible "
             "(run the relevant tests or a quick check with run_command). Create a new file with "
             "write_file(path, content) (long files: the first part, then append_file in the next steps); "
             "open(), os and subprocess are not available in your code.")
    return f"""You are Agent Smith, a careful senior software engineer working inside the user's project at {root}.
You act by writing Python code that calls tools (Python functions). The project is only reachable through
the tools, never with open() or imports.

{FORMAT_RULES}

{build_manual(config, mcp)}

{rules}{extra}

The folder agent_code/runs/ only holds logs of past agent runs (the code of each step): ignore it unless the
user asks about it. New standalone projects (a server, a tool) go in agent_code/apps/<name>/.

When you are done, call final_answer(...) with a short answer FOR THE USER in Markdown: what you found or changed
(files and why), and anything they should check. Do not paste whole files into the answer. Never reveal
secrets (API keys, passwords, .env contents) even if you read them."""


COMMAND = re.compile(r"^/(mbpp|swe|swebench)(?:\s+(\S+))?(?:\s+(\S+))?\s*$", re.I)


def parse_command(message: str) -> Optional[Dict[str, Any]]:
    """'/mbpp 2', '/swe', '/swe sympy__sympy-13480 <model>' -> run_agent_smith arguments; None for a normal message."""
    m = COMMAND.match(message.strip())
    if not m:
        return None
    args: Dict[str, Any] = {"benchmark": "mbpp" if m.group(1).lower() == "mbpp" else "swebench"}
    if m.group(2):
        args["task_id"] = m.group(2)
    if m.group(3):
        args["model"] = m.group(3)
    return args


@dataclass
class Run:
    id: str
    mode: str
    provider: str
    model: str
    message: str
    started: float = field(default_factory=time.time)
    status: str = "running"            # running | done | failed | stopped
    ended: Optional[float] = None
    answer: str = ""
    error: Optional[str] = None
    checkpoint: Optional[str] = None
    changes: List[Dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    trace: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)  # uploads given with the message

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("trace")
        return d


class AgentService:
    def __init__(self, ws: Workspace, state_dir: Path, terminals: TerminalManager,
                 publish: Callable[[Dict[str, Any]], None]):
        self.ws, self.state_dir, self.terms, self.publish = ws, Path(state_dir), terminals, publish
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "runs").mkdir(exist_ok=True)
        self.checkpoints = Checkpoints(ws, self.state_dir)
        self.scenarios = Scenarios(publish)
        self.apps = AppManager(ws, publish)
        self.media = Media(ws, self.state_dir)
        self.runs: Dict[str, Run] = {}
        self.current: Optional[str] = None
        self._cancel: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.conversation: List[Dict[str, Any]] = self._load_conversation()
        load_env_file(PROJECT_ROOT / ".env")  # provider keys of Agent Smith itself, never the project's .env

    # ------------------------------------------------------------------ conversation
    def _conv_path(self) -> Path:
        return self.state_dir / "conversation.json"

    def _load_conversation(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self._conv_path().read_text(encoding="utf-8"))
            for r in data.get("runs", []):
                run = Run(**{k: v for k, v in r.items() if k in Run.__dataclass_fields__})
                if run.status == "running":  # the server stopped mid-run
                    run.status, run.error = "failed", "interrupted (the studio was restarted)"
                self.runs[run.id] = run
            return list(data.get("messages", []))
        except (OSError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        data = {"messages": self.conversation[-200:], "runs": [asdict(r) for r in list(self.runs.values())[-100:]]}
        tmp = self._conv_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._conv_path())

    def state(self) -> Dict[str, Any]:
        return {"messages": self.conversation, "runs": {k: r.public() for k, r in self.runs.items()}, "current": self.current}

    def clear(self) -> None:
        if self.current:
            raise WorkspaceError(409, "the agent is working; stop it first")
        self.conversation = []
        self._save()

    # ------------------------------------------------------------------ models
    @staticmethod
    def models(on_catalog=None) -> Dict[str, Any]:
        """Models of config/models.json; with ``on_catalog``, also what each reads and its price (OpenRouter
        catalog, fetched in the background; ``on_catalog()`` is called when it arrives)."""
        from agent_smith import ui_jobs  # reads config/models.json and which keys exist (never their values)
        provider, model = ui_jobs.DEFAULTS["swebench"]
        models = ui_jobs.model_choices()
        chat = load_models_config().chat_default
        if chat and any((m["provider"], m["model"]) == (chat.provider, chat.model) and m["key_ok"] for m in models):
            provider, model = chat.provider, chat.model
        # models picked in the OpenRouter tab (any of its catalog), after the configured ones
        from agent_smith.studio.openrouter import load_picks
        base = next((m for m in models if m["provider"] == "openrouter"), None)
        have = {(m["provider"], m["model"]) for m in models}
        for mid in load_picks()["chat"]:
            if base and ("openrouter", mid) not in have:
                models.append({**base, "model": mid, "notes": "picked in the OpenRouter tab", "paid": not mid.endswith(":free"),
                               "picked": True})
        if not any((m["provider"], m["model"]) == (provider, model) for m in models) and models:
            # custom model list (AGENT_SMITH_MODELS_FILE): first model that has a key, else the first one
            first = next((m for m in models if m["key_ok"]), models[0])
            provider, model = first["provider"], first["model"]
        try:  # what each OpenRouter model reads (image / audio / video / file) and its price, for the picker
            from agent_smith.studio.media import Media as _M
            cat = _M.shared_catalog(block=False, on_ready=on_catalog) if on_catalog else {}
            for m in models:
                info = cat.get(m["model"]) if m["provider"] == "openrouter" else None
                m["inputs"] = info["in"] if info else ["text"]
                m["price"] = info["price"] if info else None
        except Exception:  # noqa: BLE001 - the picker works without it
            pass
        return {"models": models, "default": {"provider": provider, "model": model}}

    def _pick(self, provider: Optional[str], model: Optional[str]) -> Dict[str, Any]:
        info = self.models()
        want = (provider or info["default"]["provider"], model or info["default"]["model"])
        for m in info["models"]:
            if (m["provider"], m["model"]) == want:
                if not m["key_ok"]:
                    raise WorkspaceError(400, f"no API key for {m['provider']}: add {m['key_env']}=... to the .env of Agent Smith")
                return m
        raise WorkspaceError(400, f"unknown model {want[0]}/{want[1]}")

    # ------------------------------------------------------------------ runs
    def start(self, message: str, mode: str, provider: Optional[str] = None, model: Optional[str] = None,
              attachments: Optional[List[str]] = None) -> Run:
        message = (message or "").strip()
        files = [self.media.get(a) for a in (attachments or [])]
        if not message and files:
            message = "See the attached file(s)."
        if not message:
            raise WorkspaceError(400, "empty message")
        if mode not in ("ask", "agent"):
            raise WorkspaceError(400, "mode must be ask or agent")
        command = parse_command(message)
        m = ({"provider": "agent-smith", "model": command.get("model") or "default model", "url": ""} if command
             else self._pick(provider, model))
        with self._lock:
            if self.current:
                raise WorkspaceError(409, "the agent is already working")
            run = Run(id=uuid.uuid4().hex[:12], mode=mode, provider=m["provider"], model=m["model"], message=message,
                      attachments=files)
            run.trace = str(self.state_dir / "runs" / f"{run.id}.jsonl")
            self.runs[run.id] = run
            self.current = run.id
            self._cancel[run.id] = threading.Event()
        history = self.conversation[-2 * HISTORY_TURNS:]
        self.conversation.append({"role": "user", "text": message, "run": run.id, "mode": mode, "ts": run.started,
                                  "attachments": files})
        self._save()
        self.publish({"type": "agent-run", "run": run.public()})
        if command:
            threading.Thread(target=self._command, args=(run, command), name=f"command-{run.id}", daemon=True).start()
        else:
            threading.Thread(target=self._work, args=(run, m["url"], history), name=f"agent-{run.id}", daemon=True).start()
        return run

    def stop(self, run_id: str) -> None:
        ev = self._cancel.get(run_id)
        if ev:
            ev.set()

    def _task_prompt(self, run: Run, history: List[Dict[str, Any]]) -> str:
        context = ""
        if history:
            lines = [f"{'User' if h['role'] == 'user' else 'You (earlier answer)'}: {h['text'][:2000]}" for h in history]
            context = "Earlier in this conversation:\n" + "\n\n".join(lines) + "\n\n---\n\n"
        return f"{context}The user's request:\n{run.message}"

    def _work(self, run: Run, url: str, history: List[Dict[str, Any]]) -> None:
        def relay(_kind: str, record: Dict[str, Any]) -> None:
            if record.get("type") == "llm_response":
                run.steps = int(record.get("step") or run.steps)
                run.input_tokens += int(record.get("input_tokens") or 0)
                run.output_tokens += int(record.get("output_tokens") or 0)
            self.publish({"type": "agent", "run": run.id, "event": record})

        slug = "".join(c if c.isalnum() else "-" for c in run.message.lower())[:40].strip("-")
        sink = EventSink(run.trace, listeners=[relay, CodeLog(code_root(self.ws.root), label=slug or run.id)])
        final = "failed"  # published only once the changes are known (a finished run must list its changes)
        try:
            if run.mode == "agent":
                sink.emit("checkpoint", {"phase": "start"})
                run.checkpoint = self.checkpoints.snapshot(f"before agent turn {run.id}")
                sink.emit("checkpoint", {"phase": "done", "id": run.checkpoint})
            if self.terms.mode == "docker" and self.terms.container.available() is None:
                self.terms.container.ensure()
                env, root = {"SWEBENCH_CONTAINER": self.terms.container.name, "TESTBED_PATH": "/workspace"}, "/workspace"
            else:
                env, root = {"TESTBED_PATH": str(self.ws.root)}, str(self.ws.root)
            sink.emit("workspace", {"executor": "docker" if "SWEBENCH_CONTAINER" in env else "local", "root": root})
            llm = build_client(run.model, url, use_fallbacks=True, on_event=sink.emit)
            llm.max_wait = 15.0  # interactive: a rate-limit wait longer than this moves on to the next model
            config = load_sandbox_config(None)
            mcp = MCPClient(stdio_command=default_server("mcp_tools_swebench.py"), env=server_env(env))
            with mcp.connect():
                if run.mode == "ask":  # read-only: the tools that change things are not even offered
                    mcp.tools = [t for t in mcp.tools if t.name in READ_ONLY_TOOLS]
                    allowed, call = {t.name for t in mcp.tools}, mcp.call_tool
                    mcp.call_tool = lambda name, args: (call(name, args) if name in allowed else  # type: ignore[method-assign]
                                                        (_ for _ in ()).throw(PermissionError(f"{name} is not allowed in Ask mode")))
                else:  # Agent mode: tools served in-process by the studio, offered and called like the MCP tools
                    local: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
                    cancelled = self._cancel[run.id].is_set
                    if self.scenarios.unavailable() is None:  # Agent Smith's own benchmark runs
                        mcp.tools.append(self.scenarios.spec())
                        local[SCENARIO_TOOL] = lambda args: self.scenarios.call(args, run.id, cancelled)
                    files = FileTools(self.ws)  # whole files, also in several steps (small output limits)
                    mcp.tools.extend(file_specs())
                    for tool in FILE_TOOLS:
                        local[tool] = lambda args, tool=tool: files.call(tool, args)
                    if self.apps.unavailable() is None:  # build and run a project folder in Docker on a free port
                        mcp.tools.extend(self.apps.specs())
                        for tool in APP_TOOLS:
                            local[tool] = lambda args, tool=tool: self.apps.call(tool, args, run.id)
                    forward = mcp.call_tool
                    mcp.call_tool = lambda name, args: local[name](args) if name in local else forward(name, args)  # type: ignore[method-assign]
                sink.emit("mcp_connected", mcp.describe())
                with Sandbox(config, mcp, event_hook=sink.emit, trace_execution=True) as sandbox:
                    agent = CodeAgent(llm, sandbox, studio_system_prompt(config, mcp, root, run.mode), LIMITS, "studio", run.id,
                                      settings=SETTINGS, events=sink, cancel=self._cancel[run.id].is_set)
                    extra, parts = (self.media.for_turn([f["id"] for f in run.attachments], run.model, run.provider,
                                                        lambda s: sink.emit("attachments", {"note": s}))
                                    if run.attachments else ("", []))
                    out = agent.run(self._task_prompt(run, history) + extra, attachments=parts)
            run.answer = (out.solution or "").strip()
            stopped = self._cancel[run.id].is_set()
            final = "stopped" if stopped else ("done" if out.success else "failed")
            run.error = None if out.success else (out.error or None)
        except Exception as exc:  # noqa: BLE001 - surface any failure in the chat instead of losing it
            final, run.error = "failed", f"{type(exc).__name__}: {exc}"
            sink.emit("studio_error", {"error": run.error, "trace": traceback.format_exc()[-2000:]})
        finally:
            self._finish(run, sink, final)

    def _finish(self, run: Run, sink: EventSink, final: str) -> None:
        if run.checkpoint:
            try:
                run.changes = self.checkpoints.changes(run.checkpoint)
            except WorkspaceError:
                run.changes = []
        run.ended = time.time()
        run.status = final  # set last: a finished run already lists its changes
        text = run.answer or (run.error or "")
        self.conversation.append({"role": "assistant", "text": text, "run": run.id, "status": run.status, "ts": run.ended})
        with self._lock:
            self.current = None
        self._save()
        sink.close()
        self.publish({"type": "agent-run", "run": run.public()})

    # ------------------------------------------------------------------ chat commands (no LLM)
    def _command(self, run: Run, args: Dict[str, Any]) -> None:
        """/mbpp, /swe: Agent Smith's pipeline started at once, like a button — no model in the chat, no tokens."""
        sink = EventSink(run.trace, listeners=[lambda _k, record: self.publish({"type": "agent", "run": run.id, "event": record})])
        final = "failed"
        try:
            sink.emit("chat_command", {"command": run.message, "tool": SCENARIO_TOOL, "args": args})
            reason = self.scenarios.unavailable()
            if reason:
                run.error = f"cannot run Agent Smith here: {reason}"
                return
            out = self.scenarios.call(args, run.id, self._cancel[run.id].is_set)
            head, _, rest = out.text.partition("\n")
            run.answer = f"**{head}**" + (f"\n\n```text\n{rest.strip()}\n```" if rest.strip() else "")
            final = "stopped" if self._cancel[run.id].is_set() else ("failed" if out.is_error else "done")
            if out.is_error:
                run.error = head
        except Exception as exc:  # noqa: BLE001 - shown in the chat
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._finish(run, sink, final)

    # ------------------------------------------------------------------ review
    def get(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if not run:
            raise WorkspaceError(404, "no such run")
        return run

    def diff(self, run_id: str, rel: str) -> Dict[str, Any]:
        run = self.get(run_id)
        if not run.checkpoint:
            raise WorkspaceError(400, "this turn made no checkpoint")
        original = self.checkpoints.original(run.checkpoint, rel)
        path = self.ws.resolve(rel, must_exist=False)
        modified = None
        if path.is_file():
            f = self.ws.read(rel)
            modified = None if f["binary"] else f["content"]
        return {"path": rel, "original": original or "", "modified": modified or "", "added": original is None,
                "deleted": not path.exists()}

    def undo(self, run_id: str, paths: Optional[List[str]] = None) -> Dict[str, Any]:
        run = self.get(run_id)
        if not run.checkpoint:
            raise WorkspaceError(400, "this turn made no checkpoint")
        if self.current:
            raise WorkspaceError(409, "the agent is working; stop it first")
        restored = self.checkpoints.restore(run.checkpoint, paths)
        run.changes = self.checkpoints.changes(run.checkpoint)
        self._save()
        self.publish({"type": "agent-run", "run": run.public()})
        return {"restored": restored, "changes": run.changes}

    def trace(self, run_id: str) -> str:
        path = Path(self.get(run_id).trace)
        return path.read_text(encoding="utf-8") if path.is_file() else ""
