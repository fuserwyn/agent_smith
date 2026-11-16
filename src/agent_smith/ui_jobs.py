"""Actions the UI can launch: fixed command recipes, never arbitrary commands.

The browser sends an action name and a few parameters; this module validates them (model and
provider from ``config/models.json``, task ids by pattern / exam pool) and builds the exact argv
lists itself. The same steps are rendered as copyable shell commands, so what the UI shows is what
it runs. One job at a time; output goes to ``runs/ui/<timestamp>-<action>/``.
"""

from __future__ import annotations

import collections
import functools
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_smith.config import PROJECT_ROOT, load_models_config

ROOT = PROJECT_ROOT
MOULINETTE = ROOT / "moulinette"
JOBS_DIR = ROOT / "runs" / "ui"
TRACE_ROOTS = ("benchmarks", "evaluations", "runs")
MBPP_ID = re.compile(r"^\d{1,4}$")
SWE_ID = re.compile(r"^[A-Za-z0-9_.\-]{3,80}$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
DEFAULTS = {"mbpp": ("groq", "qwen/qwen3.8-27b"), "swebench": ("nvidia", "nvidia/nemotron-3.5-lightning-30b-a3b")}


class ActionError(ValueError):
    """Invalid action or parameters (reported to the UI as HTTP 400)."""


@dataclass
class Step:
    key: str                      # UI label key (translated client-side)
    argv: List[str]
    cwd: Path = ROOT
    env: Dict[str, str] = field(default_factory=dict)

    def display(self) -> str:
        """The command as a user would type it from the repository root."""
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in self.env.items())
        cmd = f"{prefix} {shlex.join(self.argv)}" if prefix else shlex.join(self.argv)
        rel = os.path.relpath(self.cwd, ROOT)
        return cmd if rel == "." else f"(cd {shlex.quote(rel)} && {cmd})"


# ---------------------------------------------------------------- inputs the UI can offer
def env_key_names(env_file: Path = ROOT / ".env") -> set:
    """Names of variables with a non-empty value in .env (values are never read out)."""
    names = set()
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:]
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if v.strip().strip("'\""):
                names.add(k.strip())
    names |= {k for k, v in os.environ.items() if v and re.match(r"^\w+_API_KEYS?(_\d)?$", k)}
    return names


def model_choices() -> List[Dict[str, Any]]:
    cfg, have = load_models_config(), env_key_names()
    out = []
    for m in cfg.models:
        p = cfg.provider_by_name(m.provider)
        if not p:
            continue
        prefix = p.key_env
        key_ok = prefix is None or any(n == prefix or n == prefix + "S" or n.startswith(prefix + "_") for n in have)
        out.append({"provider": p.name, "model": m.model, "url": p.base_url, "notes": m.notes or "", "key_env": prefix, "key_ok": key_ok})
    return out


def swebench_pool() -> List[str]:
    """Exam pool ids read from the moulinette sources (SEED_POOL + EXAM_POOL)."""
    src = MOULINETTE / "moulinette" / "swebench" / "interact.py"
    if not src.is_file():
        return []
    text = src.read_text(encoding="utf-8")
    ids: List[str] = []
    for name in ("SEED_POOL = [", "EXAM_POOL = SEED_POOL + ["):
        i = text.find(name)
        if i >= 0:
            ids += re.findall(r'"([^"]+)"', text[i:text.find("]", i)])
    return list(dict.fromkeys(ids))


@functools.lru_cache(maxsize=1)
def mbpp_tasks() -> List[Dict[str, Any]]:
    """MBPP task list for the picker: id, split, statement only (never the reference code or the tests)."""
    src = MOULINETTE / "moulinette" / "mbpp" / "data" / "sanitized_tasks.json"
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted(({"id": t["task_id"], "split": t.get("split", ""), "text": " ".join(str(t.get("task_definition", "")).split())}
                   for t in data if isinstance(t.get("task_id"), int)), key=lambda t: t["id"])


@functools.lru_cache(maxsize=1)
def docker_host() -> str:
    if os.environ.get("DOCKER_HOST"):
        return os.environ["DOCKER_HOST"]
    try:
        out = subprocess.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def docker_running() -> bool:
    """The moulinette validates both benchmarks in Docker. A socket file can outlive the daemon, so connect to it."""
    host = docker_host()
    if not host.startswith("unix://"):
        return bool(host)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(host[7:])
        return True
    except OSError:
        return False
    finally:
        sock.close()


def actions() -> Dict[str, Any]:
    return {"models": model_choices(), "pool": swebench_pool(), "mbpp": mbpp_tasks(), "defaults": {k: {"provider": p, "model": m} for k, (p, m) in DEFAULTS.items()},
            "moulinette": (MOULINETTE / "pyproject.toml").is_file(), "env_file": (ROOT / ".env").is_file(),
            "docker": bool(shutil.which("docker")), "docker_running": docker_running(), "uv": bool(shutil.which("uv"))}


# ---------------------------------------------------------------- recipes
def _model(params: Dict[str, Any], bench: str) -> tuple:
    want_p, want_m = params.get("provider") or DEFAULTS[bench][0], params.get("model") or DEFAULTS[bench][1]
    for m in model_choices():
        if m["provider"] == want_p and m["model"] == want_m:
            return m["model"], m["url"]
    raise ActionError(f"unknown model {want_p}/{want_m} (not in config/models.json)")


def plan(action: str, params: Dict[str, Any], job_dir: Path) -> List[Step]:
    rel = job_dir.relative_to(ROOT).as_posix()
    moul = {"PYTHONPATH": "."}
    if action == "mbpp":
        model, url = _model(params, "mbpp")
        tid = str(params.get("task_id") or "").strip()
        if tid and not MBPP_ID.match(tid):
            raise ActionError("MBPP task id must be a number")
        return [
            Step("step_dump", ["uv", "run", "moulinette_eval", "dump", "mbpp", *(["--task-id", tid] if tid else []),
                               "--output", f"../{rel}/task.json"], MOULINETTE, moul),
            Step("step_agent", ["uv", "run", "python", "-m", "agent_mbpp", "--task-file", f"{rel}/task.json",
                                "--output", f"{rel}/solution.json", "--model-name", model, "--provider-url", url,
                                "--env-file", ".env", "--trace-file", f"{rel}/events.jsonl"]),
            Step("step_validate", ["uv", "run", "moulinette_eval", "validate", "mbpp", f"../{rel}/task.json",
                                   f"../{rel}/solution.json"], MOULINETTE, {**moul, "DOCKER_HOST": docker_host()}),
        ]
    if action == "swebench":
        model, url = _model(params, "swebench")
        tid = str(params.get("task_id") or "").strip()
        if not SWE_ID.match(tid):
            raise ActionError("choose a SWE-bench task")
        return [
            Step("step_dump", ["uv", "run", "moulinette_eval", "dump", "swebench", "--task-id", tid,
                               "--output", f"../{rel}/task.json"], MOULINETTE, moul),
            Step("step_agent", ["uv", "run", "python", "-m", "agent_swebench", "--task-file", f"{rel}/task.json",
                                "--output", f"{rel}/solution.json", "--model-name", model, "--provider-url", url,
                                "--env-file", ".env", "--trace-file", f"{rel}/events.jsonl"]),
            Step("step_containers", ["docker", "ps", "-a", "--filter", "label=agent-smith"]),
            Step("step_validate", ["uv", "run", "moulinette_eval", "validate", "swebench", f"../{rel}/task.json",
                                   f"../{rel}/solution.json"], MOULINETTE, {**moul, "DOCKER_HOST": docker_host()}),
        ]
    common = ["--student-path", ".", "--moulinette-path", "./moulinette", "--env-file", ".env"]
    if action == "sandbox":
        return [Step("step_sandbox", ["./scripts/mock_exam.sh", "sandbox", *common])]
    if action == "exam":
        return [Step("step_exam", ["./scripts/mock_exam.sh", "all", *common])]
    raise ActionError(f"unknown action {action!r}")


def preview(action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    job_dir = JOBS_DIR / f"manual-{action}"  # copy-paste friendly; runs from the UI get a timestamped folder
    steps = plan(action, params, job_dir)
    return {"steps": [{"key": s.key, "command": s.display()} for s in steps],
            "dir": job_dir.relative_to(ROOT).as_posix(), "trace": f"{job_dir.relative_to(ROOT).as_posix()}/events.jsonl"
            if action in ("mbpp", "swebench") else None}


PORTABLE_DOCKER_HOST = "\"$(docker context inspect --format '{{.Endpoints.docker.Host}}')\""
STATIC_DEFAULTS = (("mbpp", {}), ("swebench", {"task_id": "sympy__sympy-13480"}), ("sandbox", {}), ("exam", {}))


def static_commands() -> Dict[str, Any]:
    """Default commands embedded in ui/index.html for when the page is opened as a file (no server).

    Same recipes as ``preview``; the machine-specific Docker socket is replaced by a portable expression.
    Refresh with ``uv run python scripts/embed_ui_commands.py`` (a test fails when they drift).
    """
    out = {}
    for action, params in STATIC_DEFAULTS:
        pv = preview(action, params)
        for step in pv["steps"]:
            step["command"] = re.sub(r"DOCKER_HOST=(?:'[^']*'|\S+)", lambda m: "DOCKER_HOST=" + PORTABLE_DOCKER_HOST, step["command"])
        out[action] = pv
    return out


# ---------------------------------------------------------------- jobs
class JobManager:
    """Runs one job at a time in a background thread; the UI polls ``snapshot()``."""

    def __init__(self, on_trace=None):
        self._lock = threading.Lock()
        self._job: Optional[Dict[str, Any]] = None
        self._proc: Optional[subprocess.Popen] = None
        self._on_trace = on_trace or (lambda path: None)

    def running(self) -> bool:
        return bool(self._job and self._job["status"] == "running")

    def start(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self.running():
                raise ActionError("another run is in progress")
            if not shutil.which("uv"):
                raise ActionError("uv is not on PATH")
            job_dir = JOBS_DIR / f"{datetime.now():%Y-%m-%d_%H-%M-%S}-{action}"
            steps = plan(action, params, job_dir)
            job_dir.mkdir(parents=True, exist_ok=True)
            trace = job_dir / "events.jsonl" if action in ("mbpp", "swebench") else None
            self._job = {"id": job_dir.name, "action": action, "params": params, "dir": job_dir.relative_to(ROOT).as_posix(),
                         "trace": trace.relative_to(ROOT).as_posix() if trace else None, "status": "running", "verdict": None,
                         "started": time.time(), "ended": None, "log": collections.deque(maxlen=800),
                         "steps": [{"key": s.key, "command": s.display(), "status": "pending", "exit_code": None} for s in steps]}
            (job_dir / "commands.sh").write_text("#!/usr/bin/env bash\n# run from the repository root\nset -e\n"
                                                 + "\n".join(s.display() for s in steps) + "\n", encoding="utf-8")
            if trace:
                self._on_trace(trace)
            threading.Thread(target=self._run, args=(self._job, steps), daemon=True).start()
            return self.snapshot()

    def _run(self, job: Dict[str, Any], steps: List[Step]) -> None:
        log = job["log"]
        for i, step in enumerate(steps):
            info = job["steps"][i]
            if job["status"] != "running":
                info["status"] = "skipped"
                continue
            info["status"] = "running"
            log.append(f"$ {step.display()}")
            out: List[str] = []
            try:
                self._proc = subprocess.Popen(step.argv, cwd=step.cwd, env={**os.environ, **step.env}, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
                for line in self._proc.stdout:
                    clean = ANSI.sub("", line.rstrip("\n"))
                    log.append(clean)
                    out.append(clean)
                    m = re.match(r"^Overall:\s+(\w+)", clean.strip())
                    if m:
                        job["verdict"] = m.group(1)
                rc = self._proc.wait()
            except OSError as exc:
                log.append(f"error: {exc}")
                rc = 127
            self._proc = None
            info["exit_code"] = rc
            folder = ROOT / job["dir"]
            with (folder / "output.log").open("a", encoding="utf-8") as fh:  # full output, the UI keeps only the tail
                fh.write(f"$ {step.display()}\n" + "\n".join(out) + f"\n[exit {rc}]\n")
            if step.key == "step_validate":  # same file name as the benchmark folders: the run list reads it
                (folder / "validation.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
            if job["status"] == "cancelled":
                info["status"] = "cancelled"
            elif rc == 0 or step.key == "step_validate":  # a failed validation is a verdict, not a crash
                info["status"] = "ok" if rc == 0 else "failed"
            else:
                info["status"] = "failed"
                job["status"] = "failed"
        if job["status"] == "running":
            job["status"] = "done"
        job["ended"] = time.time()
        if job["verdict"] is None:
            job["verdict"] = _verdict(ROOT / job["dir"])

    def cancel(self) -> Dict[str, Any]:
        with self._lock:
            if self.running():
                self._job["status"] = "cancelled"
                proc = self._proc
                if proc and proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)  # the agent removes its container on SIGTERM
                    except ProcessLookupError:
                        pass
                    threading.Timer(8, _force_kill, args=(proc,)).start()
            return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        job = self._job
        if not job:
            return {"status": "idle"}
        out = {k: v for k, v in job.items() if k != "log"}
        out["log"] = list(job["log"])[-200:]
        out["elapsed"] = round((job["ended"] or time.time()) - job["started"], 1)
        return out


def _force_kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------- recorded runs
def _verdict(folder: Path) -> Optional[str]:
    v = folder / "validation.txt"
    if v.is_file():
        m = re.search(r"^Overall:\s+(\w+)", ANSI.sub("", v.read_text(encoding="utf-8", errors="replace")), re.M)
        if m:
            return m.group(1)
    return None


def recorded_runs(limit: int = 300) -> List[Dict[str, Any]]:
    runs = []
    for base in TRACE_ROOTS:
        root = ROOT / base
        if not root.is_dir():
            continue
        for trace in root.rglob("events.jsonl"):
            if trace.stat().st_size == 0:
                continue
            first = _run_start(trace)
            if not first:
                continue
            folder = trace.parent
            runs.append({"path": trace.relative_to(ROOT).as_posix(), "group": "/".join(trace.relative_to(ROOT).parts[:-2]),
                         "benchmark": first.get("benchmark"), "task_id": str(first.get("task_id")), "model": first.get("model"),
                         "api_url": first.get("api_url"), "verdict": _verdict(folder), "mtime": trace.stat().st_mtime})
    # runs launched from the UI: newest first; benchmark folders: by task
    runs.sort(key=lambda r: (r["group"], -r["mtime"] if r["group"].startswith("runs") else 0, r["task_id"]))
    return runs[:limit]


def _run_start(trace: Path) -> Optional[Dict[str, Any]]:
    with trace.open(encoding="utf-8", errors="replace") as fh:
        for _, line in zip(range(50), fh):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "run_start":
                return ev
    return None


def safe_trace_path(rel: str) -> Path:
    """Resolve a trace path requested by the UI; only events.jsonl / solution.json under known folders."""
    p = (ROOT / rel).resolve()
    if p.name not in ("events.jsonl", "solution.json") or not any(p.is_relative_to((ROOT / b).resolve()) for b in TRACE_ROOTS):
        raise ActionError("path not allowed")
    if not p.is_file():
        raise ActionError("no such file")
    return p
