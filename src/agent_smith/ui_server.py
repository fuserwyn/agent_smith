"""``uv run agent-ui`` — serve the interactive UI, stream a live trace and launch runs.

    uv run agent-ui --trace runs/live.jsonl          # then open http://127.0.0.1:8765
    uv run python -m agent_mbpp ... --trace-file runs/live.jsonl

Endpoints
    GET  /                     ui/index.html
    GET  /api/info             app id, followed trace, whether runs can be launched
    GET  /events               Server-Sent Events: the followed trace, then every appended line
                               (?default=1: always the --trace file, for runs started in a terminal)
    GET  /api/actions          models (config/models.json, key present or not), SWE-bench exam pool
    GET  /api/runs             recorded runs (events.jsonl under benchmarks/, evaluations/, runs/)
    GET  /api/trace?path=...   one recorded events.jsonl / solution.json
    GET  /api/job              state of the current or last launched run
    POST /api/preview          commands an action would run (no side effects)
    POST /api/run              start an action (fixed recipes in ui_jobs.py)
    POST /api/cancel           stop the running action

Standard library only. Binds to localhost; launching runs is refused on any other interface.
POST requests need the ``X-Agent-Smith`` header and a matching Host/Origin, so another web page
open in the same browser cannot trigger a run.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_smith import ui_jobs
from agent_smith.config import PROJECT_ROOT

UI_FILE = PROJECT_ROOT / "ui" / "index.html"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class TraceState:
    """The trace file followed by /events; switched to the run launched from the UI."""

    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self.path, self.gen = path, 0

    def set(self, path: Path) -> None:
        with self._lock:
            self.path, self.gen = Path(path).resolve(), self.gen + 1

    def get(self):
        with self._lock:
            return self.path, self.gen


def make_handler(trace: Path, *, host: str = "127.0.0.1", port: int = 8765, jobs: ui_jobs.JobManager | None = None,
                 state: TraceState | None = None):
    state = state or TraceState(trace)
    allow_runs = host in LOOPBACK
    jobs = jobs or ui_jobs.JobManager(on_trace=state.set)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _host_ok(self) -> bool:
            # DNS-rebinding guard: only answer requests addressed to the loopback name we serve on.
            name = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return name in LOOPBACK or name == host

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._send(403, b"forbidden host", "text/plain")
            url = urlparse(self.path)
            path = url.path
            try:
                if path in ("/", "/index.html"):
                    page = UI_FILE.read_text(encoding="utf-8")
                    html = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
                            "content='width=device-width, initial-scale=1'></head><body>" + page + "</body></html>")
                    self._send(200, html.encode(), "text/html; charset=utf-8")
                elif path == "/api/info":
                    self._json({"app": "agent-smith-ui", "trace": str(state.get()[0]), "runs": allow_runs})
                elif path == "/events":
                    self._stream(fixed=trace if parse_qs(url.query).get("default") == ["1"] else None)
                elif path == "/api/actions":
                    self._json(ui_jobs.actions())
                elif path == "/api/runs":
                    self._json(ui_jobs.recorded_runs())
                elif path == "/api/trace":
                    p = ui_jobs.safe_trace_path(parse_qs(url.query).get("path", [""])[0])
                    self._send(200, p.read_bytes(), "text/plain; charset=utf-8")
                elif path == "/api/job":
                    self._json(jobs.snapshot())
                else:
                    self._send(404, b"not found", "text/plain")
            except ui_jobs.ActionError as exc:
                self._json({"error": str(exc)}, 400)

        def do_POST(self) -> None:  # noqa: N802
            origin = self.headers.get("Origin")
            if not self._host_ok() or self.headers.get("X-Agent-Smith") != "1" or (
                    origin and urlparse(origin).hostname not in LOOPBACK | {host}):
                return self._json({"error": "forbidden"}, 403)
            if not allow_runs:
                return self._json({"error": "launching runs is only allowed when serving on localhost"}, 403)
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 64_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                action, params = str(body.get("action", "")), body.get("params") or {}
                if not isinstance(params, dict):
                    raise ui_jobs.ActionError("params must be an object")
                if self.path == "/api/preview":
                    self._json(ui_jobs.preview(action, params))
                elif self.path == "/api/run":
                    self._json(jobs.start(action, params))
                elif self.path == "/api/cancel":
                    self._json(jobs.cancel())
                else:
                    self._json({"error": "not found"}, 404)
            except (ui_jobs.ActionError, ValueError) as exc:
                self._json({"error": str(exc)}, 400)

        def _stream(self, fixed: Path | None = None) -> None:
            """Follow the trace; `fixed` = always the --trace file (runs started in a terminal)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            pos, inode, gen = 0, None, state.get()[1]
            try:
                while True:
                    trace_path, cur = (fixed, gen) if fixed else state.get()
                    if cur != gen:  # a run was launched from the UI: follow its trace from the start
                        pos, inode, gen = 0, None, cur
                        self.wfile.write(b'data: {"type": "__reset"}\n\n')
                    if trace_path.exists():
                        st = trace_path.stat()
                        if inode != st.st_ino or st.st_size < pos:  # new or truncated file: start over
                            pos, inode = 0, st.st_ino
                        with trace_path.open("r", encoding="utf-8") as fh:
                            fh.seek(pos)
                            while True:
                                line = fh.readline()
                                if not line or not line.endswith("\n"):
                                    break
                                pos = fh.tell()
                                self.wfile.write(f"data: {line.strip()}\n\n".encode())
                        self.wfile.flush()
                    else:
                        self.wfile.write(b": waiting for trace\n\n")
                        self.wfile.flush()
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent-ui", description="Interactive Agent Smith visualiser")
    parser.add_argument("--trace", default=os.environ.get("AGENT_TRACE_FILE", "runs/live.jsonl"),
                        help="JSONL trace written by --trace-file (default runs/live.jsonl)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    trace = Path(args.trace).resolve()
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(trace, host=args.host, port=args.port))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(f"agent-ui: port {args.port} is already in use — the UI is probably running already: "
              f"open http://{args.host}:{args.port}\n"
              f"  or start another one:  uv run agent-ui --port {args.port + 1}\n"
              f"  or free the port:      lsof -ti tcp:{args.port} | xargs kill")
        return 1
    print(f"Agent Smith UI on http://{args.host}:{args.port}  (following {trace})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
