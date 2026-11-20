"""Agent Smith's own scenarios (an MBPP or SWE-bench run) as a tool of the studio's chat agent.

The chat agent sees ``run_agent_smith(benchmark, task_id, model)`` in its tool list next to the MCP tools
and calls it the same way (the sandbox proxies the call). The studio serves it in-process: it launches
the fixed recipe of ``agent_smith.ui_jobs`` (dump the task → run ``agent_mbpp`` / ``agent_swebench`` →
validate with the moulinette), streams the inner agent's trace to the "Inside the agent" panel while it
runs, and returns a summary (verdict, iterations, tokens, paths) to the chat agent.

Only fixed recipes run (never a command chosen by the model), one at a time, and only in Agent mode.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_smith import ui_jobs
from agent_smith.mcp_client.client import ToolOutcome, ToolParam, ToolSpec

TOOL = "run_agent_smith"
DEFAULT_SWE = dict(ui_jobs.STATIC_DEFAULTS)["swebench"]["task_id"]  # the observatory's default task
ALIASES = {"mbpp": "mbpp", "swe": "swebench", "swebench": "swebench", "swe-bench": "swebench", "swe_bench": "swebench"}
MAX_SECONDS = 25 * 60
TRANSPORT = "in-process (Agent Smith Studio)"


class Scenarios:
    def __init__(self, publish: Callable[[Dict[str, Any]], None]):
        self.publish = publish
        self.jobs = ui_jobs.JobManager()
        self.history: Dict[str, Dict[str, Any]] = {}  # job id -> public info (+ the chat run that started it)
        self.completed: Dict[str, str] = {}  # chat turn -> summary line of the run it completed (one per turn)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ offer
    def unavailable(self) -> Optional[str]:
        if not (ui_jobs.MOULINETTE / "pyproject.toml").is_file():
            return "the moulinette is not installed next to Agent Smith (needed to dump and validate tasks)"
        if not ui_jobs.shutil.which("uv"):
            return "uv is not on PATH"
        return None

    def spec(self) -> ToolSpec:
        pool = ui_jobs.swebench_pool()
        return ToolSpec(TOOL, (
            "Run Agent Smith itself on a benchmark task — the real pipeline: dump the task with the moulinette, "
            "run agent_mbpp / agent_swebench, validate the solution. Takes minutes; the user watches the inner agent "
            "live in 'Inside the agent'. Returns the verdict, iterations, tokens and where the trace and the generated "
            "code are. Use it when the user asks to run/try/test an MBPP or SWE-bench task; never start these runs "
            "with run_command, and call it directly: no need to explore the project first. One run per turn. "
            "benchmark: 'mbpp' or "
            "'swebench'. task_id: optional — an MBPP number, or a SWE-bench id "
            f"(e.g. {', '.join(pool[:3]) or DEFAULT_SWE}); omitted: the default task. "
            "model: optional model id from config/models.json (default: the benchmark's default model)."), [
            ToolParam("benchmark", "str", "'mbpp' or 'swebench'", required=True),
            ToolParam("task_id", "str", "MBPP number or SWE-bench instance id (optional)", default=None, has_default=True),
            ToolParam("model", "str", "model id from config/models.json", default=None, has_default=True),
        ])

    # ------------------------------------------------------------------ call
    def call(self, args: Dict[str, Any], chat_run: str, cancelled: Callable[[], bool]) -> ToolOutcome:
        request = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": TOOL, "arguments": args}}
        t0 = time.monotonic()

        def out(text: str, error: bool) -> ToolOutcome:
            return ToolOutcome(text, error, request=request, transport=TRANSPORT, rtt_ms=round((time.monotonic() - t0) * 1000, 1))

        if chat_run in self.completed:  # small models like to "also try" more tasks: one completed run per turn
            return out(f"Already done in this turn: {self.completed[chat_run]}. Report that result to the user now "
                       "(call final_answer); they can ask again for another run.", True)
        action = ALIASES.get(str(args.get("benchmark") or "").strip().lower())
        if not action:
            return out("benchmark must be 'mbpp' or 'swebench'", True)
        reason = self.unavailable()
        if reason:
            return out(f"cannot run Agent Smith here: {reason}", True)
        params: Dict[str, Any] = {}
        if args.get("task_id") not in (None, ""):
            params["task_id"] = str(args["task_id"]).strip()
            if action == "mbpp":  # the ids have gaps (no 1, 5, 10…): answer at once instead of a failed dump
                known = [t["id"] for t in ui_jobs.mbpp_tasks()]
                if known and params["task_id"].isdigit() and int(params["task_id"]) not in known:
                    want = int(params["task_id"])
                    near = sorted(known, key=lambda i: abs(i - want))[:6]
                    return out(f"MBPP task {want} does not exist ({len(known)} tasks, ids {known[0]}–{known[-1]} with gaps). "
                               f"Nearest ids: {', '.join(map(str, sorted(near)))}. Or omit task_id for the default task.", True)
        elif action == "swebench":
            params["task_id"] = DEFAULT_SWE
        if args.get("model"):
            want = str(args["model"]).strip()
            match = [m for m in ui_jobs.model_choices() if m["model"] == want]
            if not match and "/" in want and ui_jobs.load_models_config().provider_by_name("openrouter"):
                match = [{"provider": "openrouter", "model": want}]  # any OpenRouter model (e.g. picked in its tab)
            if not match:
                return out(f"unknown model {args['model']!r}; models: "
                           + ", ".join(m["model"] for m in ui_jobs.model_choices() if m["key_ok"]), True)
            params.update(provider=match[0]["provider"], model=match[0]["model"])
        try:
            snap = self.jobs.start(action, params)
        except ui_jobs.ActionError as exc:
            return out(f"cannot start: {exc}", True)
        job_id = snap["id"]
        self._update(job_id, snap, chat_run, started=True)
        deadline = time.monotonic() + MAX_SECONDS
        last = ""
        while True:
            snap = self.jobs.snapshot()
            if snap.get("status") != "running":
                break
            if cancelled() or time.monotonic() > deadline:
                self.jobs.cancel()
            sig = json.dumps([s["status"] for s in snap.get("steps", [])])
            if sig != last:
                self._update(job_id, snap, chat_run)
                last = sig
            time.sleep(0.5)
        self._update(job_id, snap, chat_run)
        summary = self._summary(snap)
        if snap.get("status") == "done":
            self.completed[chat_run] = summary.split("\n", 1)[0]
        return out(summary, snap.get("status") != "done")

    def _update(self, job_id: str, snap: Dict[str, Any], chat_run: str, started: bool = False) -> None:
        info = {"id": job_id, "run": chat_run, "action": snap.get("action"), "params": snap.get("params") or {},
                "status": snap.get("status"), "verdict": snap.get("verdict"), "trace": snap.get("trace"),
                "dir": snap.get("dir"), "started": snap.get("started"), "ended": snap.get("ended"),
                "steps": [{"key": s["key"], "status": s["status"]} for s in snap.get("steps", [])]}
        with self._lock:
            self.history[job_id] = info
        self.publish({"type": "scenario", "job": info, "started": started})

    def _summary(self, snap: Dict[str, Any]) -> str:
        folder = ui_jobs.ROOT / snap["dir"]
        lines = [f"Agent Smith {snap['action']} run {snap['id']}: {snap['status']}"
                 + (f", verdict {snap['verdict']}" if snap.get("verdict") else ""),
                 "steps: " + " · ".join(f"{s['key'].removeprefix('step_')} {s['status']}" for s in snap.get("steps", []))]
        try:
            sol = json.loads((folder / "solution.json").read_text(encoding="utf-8"))
            lines.append(f"solution: success={sol.get('success')} iterations={sol.get('iterations')} "
                         f"tokens in={sol.get('total_input_tokens')} out={sol.get('total_output_tokens')} "
                         f"time={sol.get('total_time_seconds')}s" + (f" error={sol['error']}" if sol.get("error") else ""))
            if sol.get("solution"):
                lines.append("answer:\n" + str(sol["solution"])[:1500])
        except (OSError, ValueError):
            pass
        lines.append(f"files: {snap['dir']}/ (task.json, solution.json, events.jsonl, output.log); "
                     "the code of each step: agent_code/runs/ in the Agent Smith repository")
        tail = [ln for ln in snap.get("log", []) if ln.strip()][-25:]
        if tail:
            lines.append("last output lines:\n" + "\n".join(tail))
        if snap.get("status") == "done":
            lines.append("This run is complete: report its result to the user. Do not start other runs unless the user asked for several.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ API
    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted(self.history.values(), key=lambda j: j.get("started") or 0, reverse=True)

    def trace(self, job_id: str, since: int = 0) -> Dict[str, Any]:
        with self._lock:
            job = self.history.get(job_id)
        if not job:
            raise KeyError(job_id)
        events: List[Dict[str, Any]] = []
        try:
            path: Optional[Path] = ui_jobs.safe_trace_path(job["trace"]) if job.get("trace") else None
        except ui_jobs.ActionError:  # not written yet (the task is still being dumped)
            path = None
        if path:
            with path.open(encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i < since:
                        continue
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        break  # a line being written: read it next time
        return {"job": job, "since": since, "events": events}
