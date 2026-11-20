"""Apps the chat agent builds and runs: a project folder with a Dockerfile, served on a free local port.

The agent writes a whole project (code, Dockerfile, dependencies) into one folder — by default
``agent_code/apps/<name>/`` — then calls these tools, offered like its MCP tools (Agent mode only):

* ``run_app(path, port, container_port)`` — ``docker build`` the folder and run it, published on
  ``127.0.0.1:<free port>`` only; waits until it answers and returns the URL, the first response and the logs;
* ``call_app(path, route, method, body)`` — an HTTP request to that app (and only to apps started here);
* ``app_logs(path, lines)`` and ``stop_app(path)``.

The studio does the Docker work itself: the agent's shell lives in the project container, which has no
Docker. Containers are limited (memory, CPU, processes, no capabilities, no privilege escalation), labelled,
and removed when they are stopped or when the studio exits.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent_smith.mcp_client.client import ToolOutcome, ToolParam, ToolSpec
from agent_smith.studio.workspace import Workspace, WorkspaceError

TOOLS = ("run_app", "call_app", "app_logs", "stop_app")
APPS_DIR = "agent_code/apps"
LABEL = "agent-smith-studio.app"
TRANSPORT = "in-process (Agent Smith Studio)"
LIMITS = ["--memory", "512m", "--cpus", "1", "--pids-limit", "256", "--cap-drop", "ALL",
          "--security-opt", "no-new-privileges"]
BUILD_TIMEOUT, READY_TIMEOUT = 600, 30
Docker = Callable[..., subprocess.CompletedProcess]


def real_docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def exposed_port(dockerfile: str) -> Optional[int]:
    m = re.search(r"^\s*EXPOSE\s+(\d+)", dockerfile, re.M | re.I)
    return int(m.group(1)) if m else None


@dataclass
class App:
    id: str
    path: str                 # folder, relative to the workspace
    name: str
    image: str
    container: str
    port: int                 # on 127.0.0.1
    container_port: int
    run: Optional[str] = None  # chat turn that (re)started it
    status: str = "starting"   # starting | running | failed | stopped
    started: float = field(default_factory=time.time)
    error: Optional[str] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def public(self) -> Dict[str, Any]:
        return {**asdict(self), "url": self.url}


class AppManager:
    def __init__(self, ws: Workspace, publish: Callable[[Dict[str, Any]], None], docker: Docker = real_docker):
        self.ws, self.publish, self.docker = ws, publish, docker
        self.apps: Dict[str, App] = {}  # by path
        self._lock = threading.Lock()
        self._docker_ok: Optional[tuple] = None
        self.tag = hashlib.sha1(str(ws.root).encode()).hexdigest()[:8]
        atexit.register(self.stop_all)

    # ------------------------------------------------------------------ offer
    def unavailable(self) -> Optional[str]:
        now = time.monotonic()
        if self._docker_ok and now - self._docker_ok[0] < 15:
            return self._docker_ok[1]
        reason = None
        if self.docker is real_docker and not shutil.which("docker"):
            reason = "Docker is not installed"
        else:
            try:
                if self.docker("info", "--format", "{{.ServerVersion}}", timeout=6).returncode != 0:
                    reason = "Docker is not running"
            except (OSError, subprocess.TimeoutExpired):
                reason = "Docker does not answer"
        self._docker_ok = (now, reason)
        return reason

    def specs(self) -> List[ToolSpec]:
        p = lambda n, t, d, req=False, default=None: ToolParam(n, t, d, required=req, default=default, has_default=not req)  # noqa: E731
        return [
            ToolSpec("run_app", (
                "Build and run a project folder as a Docker container, served on a free local port. The folder must "
                f"contain a Dockerfile (EXPOSE the port the app listens on, bind to 0.0.0.0). Put new projects in "
                f"'{APPS_DIR}/<name>/' (one folder = one coherent project: code, Dockerfile, dependencies); create each file "
                "with edit_file(path, '', content) — open() and os are not available in your code. Calling it again "
                "rebuilds and restarts the app (same URL when possible). Waits until the app answers and returns its URL, "
                "the first HTTP response and the container logs. port: a host port if the user asked for one, else omit."),
                [p("path", "str", "project folder (relative to the workspace)", True), p("port", "int", "host port (default: a free one)"),
                 p("container_port", "int", "port inside the container (default: EXPOSE in the Dockerfile, else 8000)")]),
            ToolSpec("call_app", "Send an HTTP request to an app started with run_app and return status, content type and body.",
                     [p("path", "str", "the app's project folder", True), p("route", "str", "e.g. '/' or '/items?x=1'", default="/"),
                      p("method", "str", "GET, POST, PUT, PATCH or DELETE", default="GET"),
                      p("body", "Any", "request body: a str, or a dict/list sent as JSON")]),
            ToolSpec("app_logs", "The last lines of an app's container logs (stdout and stderr).",
                     [p("path", "str", "the app's project folder", True), p("lines", "int", "how many lines", default=80)]),
            ToolSpec("stop_app", "Stop and remove an app started with run_app.", [p("path", "str", "the app's project folder", True)]),
        ]

    def call(self, name: str, args: Dict[str, Any], run: Optional[str]) -> ToolOutcome:
        request = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name, "arguments": args}}
        t0 = time.monotonic()
        try:
            handler = {"run_app": self._run_app, "call_app": self._call_app, "app_logs": self._logs, "stop_app": self._stop}[name]
            text, error = handler(args, run)
        except WorkspaceError as exc:
            text, error = str(exc), True
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError) as exc:
            text, error = f"{type(exc).__name__}: {exc}", True
        return ToolOutcome(text, error, request=request, transport=TRANSPORT, rtt_ms=round((time.monotonic() - t0) * 1000, 1))

    # ------------------------------------------------------------------ tools
    def _folder(self, args: Dict[str, Any]) -> tuple:
        rel = str(args.get("path") or "").strip()
        # the agent sees the project as /workspace (Docker shell) or as its real path (local shell)
        for prefix in ("/workspace/", str(self.ws.root).rstrip("/") + "/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
        rel = rel.strip("/")
        if not rel:
            raise ValueError("path is required (the project folder, e.g. agent_code/apps/hello)")
        folder = self.ws.resolve(rel)
        if not folder.is_dir():
            raise ValueError(f"{rel} is not a folder")
        return rel, folder

    def _app(self, args: Dict[str, Any]) -> App:
        rel, _ = self._folder(args)
        app = self.apps.get(rel)
        if not app or app.status not in ("running", "starting"):
            raise ValueError(f"no app running from {rel}: start it with run_app('{rel}')")
        return app

    def _run_app(self, args: Dict[str, Any], run: Optional[str]) -> tuple:
        rel, folder = self._folder(args)
        dockerfile = folder / "Dockerfile"
        if not dockerfile.is_file():
            return (f"{rel}/Dockerfile not found: write one first (FROM python:3.11-slim, COPY, RUN pip install …, "
                    "EXPOSE <port>, CMD [...] listening on 0.0.0.0)"), True
        reason = self.unavailable()
        if reason:
            return f"cannot run apps: {reason}", True
        cport = int(args.get("container_port") or exposed_port(dockerfile.read_text(errors="replace")) or 8000)
        old = self.apps.get(rel)
        if old:
            self._remove(old)
        want = args.get("port")
        if want:
            port = int(want)
            if not 1024 <= port <= 65535:
                return "port must be between 1024 and 65535", True
            if not port_free(port):
                return f"port {port} is already in use on this machine; omit port to get a free one", True
        else:
            port = old.port if old and port_free(old.port) else free_port()
        slug = re.sub(r"[^a-z0-9]+", "-", folder.name.lower()).strip("-") or "app"
        app = App(id=f"{self.tag}-{slug}", path=rel, name=folder.name, image=f"agent-studio-app-{self.tag}-{slug}",
                  container=f"agent-studio-app-{self.tag}-{slug}", port=port, container_port=cport, run=run)
        with self._lock:
            self.apps[rel] = app
        self._publish(app)
        build = self.docker("build", "-t", app.image, "--label", f"{LABEL}=1", str(folder), timeout=BUILD_TIMEOUT)
        if build.returncode != 0:
            return self._fail(app, "docker build failed", (build.stderr or build.stdout)[-3000:])
        started = self.docker("run", "-d", "--name", app.container, "--label", f"{LABEL}=1", *LIMITS,
                              "-p", f"127.0.0.1:{port}:{cport}", app.image, timeout=60)
        if started.returncode != 0:
            return self._fail(app, "docker run failed", started.stderr[-3000:])
        first = self._wait_ready(app)
        logs = self._tail(app, 40)
        if first is None:
            return self._fail(app, f"the app did not answer on port {cport} inside the container within {READY_TIMEOUT}s "
                                   "(does it listen on 0.0.0.0 and on the EXPOSEd port?)", logs)
        app.status = "running"
        self._publish(app)
        return (f"{app.name} is running at {app.url} (container port {cport}, container {app.container}).\n"
                f"First response: GET / -> {first}\nlogs:\n{logs or '(empty)'}"), False

    def _wait_ready(self, app: App) -> Optional[str]:
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if not self._alive(app):
                return None
            try:
                status, ctype, body = self._http(app, "/", "GET", None, timeout=3)
                return f"{status} {ctype} {body[:200]!r}"
            except (urllib.error.URLError, OSError, ConnectionError):
                time.sleep(0.5)
        return None

    def _alive(self, app: App) -> bool:
        r = self.docker("inspect", "-f", "{{.State.Running}}", app.container, timeout=15)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def _http(self, app: App, route: str, method: str, body: Any, timeout: float = 10) -> tuple:
        data, headers = None, {}
        if body is not None:
            if isinstance(body, (dict, list)):
                data, headers = json.dumps(body).encode(), {"Content-Type": "application/json"}
            else:
                data = str(body).encode()
        req = urllib.request.Request(app.url + ("/" + route.lstrip("/")), data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 - only 127.0.0.1:<app port>
                return res.status, res.headers.get("Content-Type", ""), res.read(8000).decode(errors="replace")
        except urllib.error.HTTPError as exc:  # an answer, even if 4xx/5xx
            return exc.code, exc.headers.get("Content-Type", ""), exc.read(8000).decode(errors="replace")

    def _call_app(self, args: Dict[str, Any], _run: Optional[str]) -> tuple:
        app = self._app(args)
        method = str(args.get("method") or "GET").upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return "method must be GET, POST, PUT, PATCH or DELETE", True
        route = str(args.get("route") or "/")
        if "://" in route:
            return "route is a path on the app, e.g. '/items' (not a URL)", True
        try:
            status, ctype, body = self._http(app, route, method, args.get("body"))
        except (urllib.error.URLError, OSError) as exc:
            return f"{method} {route} failed: {exc}. Logs:\n{self._tail(app, 30)}", True
        return f"{method} {app.url}/{route.lstrip('/')} -> {status} {ctype}\n{body}", False

    def _logs(self, args: Dict[str, Any], _run: Optional[str]) -> tuple:
        rel, _ = self._folder(args)
        app = self.apps.get(rel)
        if not app:
            return f"no app was started from {rel}", True
        return self._tail(app, int(args.get("lines") or 80)) or "(no output yet)", False

    def _stop(self, args: Dict[str, Any], _run: Optional[str]) -> tuple:
        app = self._app(args)
        self._remove(app)
        app.status = "stopped"
        self._publish(app)
        return f"{app.name} stopped ({app.url} is free again)", False

    # ------------------------------------------------------------------ helpers
    def _tail(self, app: App, lines: int) -> str:
        r = self.docker("logs", "--tail", str(max(1, min(lines, 1000))), app.container, timeout=20)
        return ((r.stdout or "") + (r.stderr or "")).strip()[-6000:]

    def _fail(self, app: App, what: str, details: str) -> tuple:
        app.status, app.error = "failed", what
        self._remove(app)
        self._publish(app)
        return f"{what}:\n{details.strip()}", True

    def _remove(self, app: App) -> None:
        with contextlib.suppress(Exception):
            self.docker("rm", "-f", app.container, timeout=30)

    def _publish(self, app: App) -> None:
        self.publish({"type": "app", "app": app.public()})

    # ------------------------------------------------------------------ API
    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted((a.public() for a in self.apps.values()), key=lambda a: a["started"], reverse=True)

    def by_id(self, app_id: str) -> App:
        for app in self.apps.values():
            if app.id == app_id:
                return app
        raise WorkspaceError(404, "unknown app")

    def stop_by_id(self, app_id: str) -> Dict[str, Any]:
        app = self.by_id(app_id)
        self._remove(app)
        app.status = "stopped"
        self._publish(app)
        return app.public()

    def logs_by_id(self, app_id: str, lines: int = 300) -> str:
        return self._tail(self.by_id(app_id), lines)

    def stop_all(self) -> None:
        """On exit: remove the containers, and the images too (kept while the studio runs: fast restarts)."""
        for app in list(self.apps.values()):
            if app.status in ("running", "starting"):
                self._remove(app)
                app.status = "stopped"
        for image in {a.image for a in self.apps.values()}:
            with contextlib.suppress(Exception):
                self.docker("rmi", "-f", image, timeout=60)
        self.apps.clear()
