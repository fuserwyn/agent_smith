"""``uv run agent-ui`` — serve the interactive UI and stream a live trace.

    uv run agent-ui --trace runs/live.jsonl          # then open http://127.0.0.1:8765
    uv run python -m agent_mbpp ... --trace-file runs/live.jsonl

Endpoints: ``/`` (ui/index.html), ``/api/info``, ``/events`` (Server-Sent Events:
the whole trace file, then every new line as it is appended).
Standard library only; binds to localhost by default.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_smith.config import PROJECT_ROOT

UI_FILE = PROJECT_ROOT / "ui" / "index.html"


def make_handler(trace: Path):
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

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                page = UI_FILE.read_text(encoding="utf-8")
                html = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
                        "content='width=device-width, initial-scale=1'></head><body>" + page + "</body></html>")
                self._send(200, html.encode(), "text/html; charset=utf-8")
            elif path == "/api/info":
                self._send(200, json.dumps({"app": "agent-smith-ui", "trace": str(trace)}).encode(), "application/json")
            elif path == "/events":
                self._stream()
            else:
                self._send(404, b"not found", "text/plain")

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            pos, inode = 0, None
            try:
                while True:
                    if trace.exists():
                        st = trace.stat()
                        if inode != st.st_ino or st.st_size < pos:  # new or truncated file: start over
                            pos, inode = 0, st.st_ino
                        with trace.open("r", encoding="utf-8") as fh:
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
    server = ThreadingHTTPServer((args.host, args.port), make_handler(trace))
    print(f"Agent Smith UI on http://{args.host}:{args.port}  (following {trace})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
