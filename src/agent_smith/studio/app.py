"""FastAPI application of Agent Smith Studio (local IDE around the agent framework)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_smith.config import PROJECT_ROOT
from agent_smith.studio import __version__, git
from agent_smith.studio.events import EventHub
from agent_smith.studio.security import GuardMiddleware
from agent_smith.studio.terminal import DEFAULT_IMAGE, TerminalError, TerminalManager
from agent_smith.studio.workspace import Workspace, WorkspaceError

WEB_DIST = PROJECT_ROOT / "web" / "dist"

NOT_BUILT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Agent Smith Studio</title>
<style>body{font:15px/1.5 system-ui,sans-serif;background:#0d1117;color:#e6edf3;display:grid;place-items:center;height:100vh;margin:0}
main{max-width:560px;padding:24px}code{background:#161b22;padding:2px 6px;border-radius:6px}</style></head>
<body><main><h1>Agent Smith Studio</h1><p>The server is running, but the web app is not built yet.</p>
<p>Build it once with <code>make studio-build</code>, or develop with hot reload: <code>make studio-dev</code>.</p></main></body></html>"""


class WriteBody(BaseModel):
    path: str
    content: str
    base_hash: Optional[str] = Field(None, description="hash the editor loaded; a different file on disk is a conflict")


class CreateBody(BaseModel):
    path: str
    kind: str = Field("file", pattern="^(file|dir)$")
    content: str = ""


class RenameBody(BaseModel):
    src: str = Field(alias="from")
    dst: str = Field(alias="to")


class PathBody(BaseModel):
    path: str


class TermBody(BaseModel):
    cols: int = Field(100, ge=2, le=1000)
    rows: int = Field(30, ge=2, le=500)


def _docker_state() -> Dict[str, Any]:
    if not shutil.which("docker"):
        return {"installed": False, "running": False}
    try:
        ok = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=4).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return {"installed": True, "running": ok}


def create_app(workspace: Path, token: str, web_dist: Optional[Path] = WEB_DIST, *,
               shell: str = "docker", image: str = DEFAULT_IMAGE) -> FastAPI:
    ws = Workspace(Path(workspace))
    hub = EventHub(ws)
    terms = TerminalManager(ws.root, mode=shell, image=image)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await hub.close()
        terms.close_all()

    app = FastAPI(title="Agent Smith Studio", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.workspace, app.state.hub, app.state.terminals = ws, hub, terms

    @app.exception_handler(WorkspaceError)
    @app.exception_handler(TerminalError)
    async def known_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=getattr(exc, "status", 400))

    # ---------------------------------------------------------------- session
    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return {"app": "agent-smith-studio", "version": __version__, "workspace": ws.root.name}

    @app.get("/api/session")
    def session() -> Dict[str, Any]:
        return {
            "workspace": {"name": ws.root.name, "path": str(ws.root), "git": git.is_repo(ws.root)},
            "docker": _docker_state(),
            "platform": {"system": platform.system(), "python": platform.python_version()},
        }

    # ---------------------------------------------------------------- files
    @app.get("/api/fs/list")
    def fs_list(path: str = "") -> Dict[str, Any]:
        return {"path": path, "entries": ws.list_dir(path)}

    @app.get("/api/fs/read")
    def fs_read(path: str) -> Dict[str, Any]:
        return ws.read(path)

    @app.put("/api/fs/write")
    def fs_write(body: WriteBody) -> Dict[str, Any]:
        return ws.write(body.path, body.content, body.base_hash)

    @app.post("/api/fs/create")
    def fs_create(body: CreateBody) -> Dict[str, Any]:
        return ws.mkdir(body.path) if body.kind == "dir" else ws.write(body.path, body.content, create=True)

    @app.post("/api/fs/rename")
    def fs_rename(body: RenameBody) -> Dict[str, Any]:
        return ws.rename(body.src, body.dst)

    @app.post("/api/fs/delete")
    def fs_delete(body: PathBody) -> Dict[str, Any]:
        ws.delete(body.path)
        return {"ok": True}

    @app.get("/api/fs/search")
    def fs_search(q: str = Query(..., max_length=500), regex: bool = False, case: bool = False) -> Dict[str, Any]:
        return ws.search(q, regex=regex, case=case)

    @app.get("/api/fs/files")
    def fs_files() -> Dict[str, Any]:
        return ws.files()

    # ---------------------------------------------------------------- git
    @app.get("/api/git/status")
    def git_status() -> Dict[str, Any]:
        return git.status(ws)

    @app.get("/api/git/diff")
    def git_diff(path: str) -> Dict[str, Any]:
        return git.diff(ws, path)

    # ---------------------------------------------------------------- terminals
    @app.get("/api/terminals")
    async def term_status() -> Dict[str, Any]:
        return await asyncio.to_thread(terms.status)

    @app.post("/api/terminals")
    async def term_create(body: TermBody) -> Dict[str, Any]:
        return (await terms.create(body.cols, body.rows)).info()

    @app.delete("/api/terminals/{sid}")
    async def term_kill(sid: str) -> Dict[str, Any]:
        terms.kill(sid)
        return {"ok": True}

    @app.websocket("/ws/terminal/{sid}")
    async def term_socket(socket: WebSocket, sid: str) -> None:
        try:
            s = terms.get(sid)
        except TerminalError:  # refuse the handshake itself
            await socket.close(code=4404, reason="no such terminal")
            return
        await socket.accept()
        q: asyncio.Queue = asyncio.Queue()
        s.listeners.add(q)

        async def pump() -> None:  # shell output -> browser (binary frames: xterm decodes UTF-8 itself)
            if s.buffer:
                await socket.send_bytes(b"".join(s.buffer))
            if s.exit_code is not None:
                await socket.send_json({"type": "exit", "code": s.exit_code})
                return
            while True:
                data = await q.get()
                if data is None:
                    await socket.send_json({"type": "exit", "code": s.exit_code})
                    return
                chunk = [data]
                while not q.empty() and len(chunk) < 64:  # coalesce bursts (fast output)
                    nxt = q.get_nowait()
                    if nxt is None:
                        q.put_nowait(None)
                        break
                    chunk.append(nxt)
                await socket.send_bytes(b"".join(chunk))

        sender = asyncio.create_task(pump())
        try:
            while True:
                msg = await socket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                text = msg.get("text")
                if not text:
                    continue
                with contextlib.suppress(ValueError, KeyError, TypeError, TerminalError):
                    m = json.loads(text)
                    if m["type"] == "input" and isinstance(m.get("data"), str) and len(m["data"]) <= 65536:
                        terms.write(sid, m["data"])
                    elif m["type"] == "resize":
                        terms.resize(sid, int(m["cols"]), int(m["rows"]))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sender.cancel()
            s.listeners.discard(q)

    # ---------------------------------------------------------------- live events
    @app.websocket("/ws/events")
    async def events(socket: WebSocket) -> None:
        await socket.accept()
        q = hub.subscribe()

        async def reader() -> None:  # only watches for the disconnect; the client sends nothing yet
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                while (await socket.receive())["type"] != "websocket.disconnect":
                    pass

        gone = asyncio.create_task(reader())
        try:
            await socket.send_json({"type": "hello", "workspace": ws.root.name})
            while True:
                getter = asyncio.ensure_future(q.get())
                done, _ = await asyncio.wait({getter, gone}, timeout=25, return_when=asyncio.FIRST_COMPLETED)
                if gone in done:
                    getter.cancel()
                    break
                if getter in done:
                    await socket.send_json(getter.result())
                else:
                    getter.cancel()
                    await socket.send_json({"type": "ping"})  # keeps proxies from closing an idle socket
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            gone.cancel()
            hub.unsubscribe(q)

    # ---------------------------------------------------------------- web app
    if web_dist is not None and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def not_built() -> str:
            return NOT_BUILT

    app.add_middleware(GuardMiddleware, token=token)
    return app
