"""``agent-studio [PROJECT_DIR]`` — start Agent Smith Studio on this machine.

    uv run --extra studio agent-studio .                 # opens http://127.0.0.1:8790/?token=...
    uv run --extra studio agent-studio ~/code/app --port 8791 --no-browser
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from agent_smith.studio.security import LOOPBACK, new_token


def _port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agent-studio", description="Agent Smith Studio: editor, terminal and agent chat")
    p.add_argument("workspace", nargs="?", default=".", help="project folder to open (default: current folder)")
    p.add_argument("--host", default="127.0.0.1", help="loopback address to bind (127.0.0.1, localhost or ::1)")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--token", default=os.environ.get("STUDIO_TOKEN"), help="session token (default: random)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = p.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"agent-studio: {workspace} is not a folder", file=sys.stderr)
        return 2
    if args.host not in LOOPBACK:
        print("agent-studio: the studio reads and writes files and runs a terminal; it only serves on this machine "
              f"({', '.join(sorted(LOOPBACK))})", file=sys.stderr)
        return 2
    if not _port_free(args.host, args.port):
        print(f"agent-studio: port {args.port} is already in use — the studio is probably running already,\n"
              f"  or start another one:  agent-studio --port {args.port + 1}", file=sys.stderr)
        return 1
    try:
        import uvicorn

        from agent_smith.studio.app import create_app
    except ImportError:
        print("agent-studio: the studio dependencies are missing: uv sync --extra studio", file=sys.stderr)
        return 1

    token = args.token or new_token()
    shown = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{shown}:{args.port}/?token={token}"
    print(f"Agent Smith Studio — project {workspace}\n  open: {url}\n  (the token in the link is the key to this session; Ctrl+C stops the studio)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    uvicorn.run(create_app(workspace, token), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
