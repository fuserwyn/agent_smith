"""FastAPI application of Agent Smith Studio (local IDE around the agent framework)."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agent_smith.config import PROJECT_ROOT
from agent_smith.studio import __version__
from agent_smith.studio.security import GuardMiddleware

WEB_DIST = PROJECT_ROOT / "web" / "dist"

NOT_BUILT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Agent Smith Studio</title>
<style>body{font:15px/1.5 system-ui,sans-serif;background:#0d1117;color:#e6edf3;display:grid;place-items:center;height:100vh;margin:0}
main{max-width:560px;padding:24px}code{background:#161b22;padding:2px 6px;border-radius:6px}</style></head>
<body><main><h1>Agent Smith Studio</h1><p>The server is running, but the web app is not built yet.</p>
<p>Build it once with <code>make studio-build</code>, or develop with hot reload: <code>make studio-dev</code>.</p></main></body></html>"""


def _docker_state() -> Dict[str, Any]:
    if not shutil.which("docker"):
        return {"installed": False, "running": False}
    try:
        ok = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=4).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return {"installed": True, "running": ok}


def create_app(workspace: Path, token: str, web_dist: Optional[Path] = WEB_DIST) -> FastAPI:
    workspace = Path(workspace).resolve()
    app = FastAPI(title="Agent Smith Studio", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workspace = workspace

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return {"app": "agent-smith-studio", "version": __version__, "workspace": workspace.name}

    @app.get("/api/session")
    def session() -> Dict[str, Any]:
        return {
            "workspace": {"name": workspace.name, "path": str(workspace), "git": (workspace / ".git").exists()},
            "docker": _docker_state(),
            "platform": {"system": platform.system(), "python": platform.python_version()},
        }

    if web_dist is not None and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def not_built() -> str:
            return NOT_BUILT

    app.add_middleware(GuardMiddleware, token=token)
    return app
