"""FastAPI application of Agent Smith Studio (local IDE around the agent framework)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_smith.config import PROJECT_ROOT, load_models_config
from agent_smith.studio import __version__, git
from agent_smith.studio.agent import AgentService
from agent_smith.studio.events import EventHub
from agent_smith.studio.media import MAX_UPLOAD
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


class PicksBody(BaseModel):
    chat: List[str] = Field(default_factory=list, max_length=40)
    media: Dict[str, str] = Field(default_factory=dict)


class AskBody(BaseModel):
    message: str = Field(..., max_length=20000)
    mode: str = Field("agent", pattern="^(ask|agent)$")
    provider: Optional[str] = None
    model: Optional[str] = None
    attachments: List[str] = Field(default_factory=list, max_length=20)  # ids from /api/uploads


class UndoBody(BaseModel):
    paths: Optional[list[str]] = None


def default_state_dir(root: Path) -> Path:
    """Studio data of a project (chat, traces, checkpoints): outside the project, per user."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "agent-smith-studio" / hashlib.sha1(str(root).encode()).hexdigest()[:12]


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
               shell: str = "docker", image: str = DEFAULT_IMAGE, state_dir: Optional[Path] = None) -> FastAPI:
    ws = Workspace(Path(workspace))
    hub = EventHub(ws)
    terms = TerminalManager(ws.root, mode=shell, image=image)
    loop_box: Dict[str, asyncio.AbstractEventLoop] = {}

    def publish_threadsafe(message: Dict[str, Any]) -> None:  # agent runs live in worker threads
        loop = loop_box.get("loop")
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(hub.publish, message)

    agent = AgentService(ws, state_dir or default_state_dir(ws.root), terms, publish_threadsafe)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop_box["loop"] = asyncio.get_running_loop()
        yield
        for run_id in list(agent._cancel):
            agent.stop(run_id)
        await hub.close()
        terms.close_all()
        agent.apps.stop_all()

    app = FastAPI(title="Agent Smith Studio", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.workspace, app.state.hub, app.state.terminals, app.state.agent = ws, hub, terms, agent

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
            "shell": shell,
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

    # ---------------------------------------------------------------- code navigation (go to definition, references)
    @app.get("/api/code/definition")
    async def code_definition(name: str = Query(..., max_length=120), path: Optional[str] = Query(None, max_length=1024)) -> Dict[str, Any]:
        from agent_smith.studio import navigate
        return await asyncio.to_thread(navigate.definitions, ws, name, path)

    @app.get("/api/code/references")
    async def code_references(name: str = Query(..., max_length=120), path: Optional[str] = Query(None, max_length=1024)) -> Dict[str, Any]:
        from agent_smith.studio import navigate
        return await asyncio.to_thread(navigate.references, ws, name, path)

    # ---------------------------------------------------------------- git
    @app.get("/api/git/status")
    def git_status() -> Dict[str, Any]:
        return git.status(ws)

    @app.get("/api/git/diff")
    def git_diff(path: str) -> Dict[str, Any]:
        return git.diff(ws, path)

    # ---------------------------------------------------------------- OpenRouter tab
    @app.get("/api/openrouter/catalog")
    async def or_catalog() -> Dict[str, Any]:
        from agent_smith.studio import openrouter
        models = await asyncio.to_thread(openrouter.catalog)
        return {"models": models, "picks": openrouter.load_picks(), "media_defaults": {
            k: [e["model"] for e in (load_models_config().media.get(k) or [])] for k in openrouter.MEDIA_KINDS}}

    @app.get("/api/openrouter/account")
    async def or_account() -> Dict[str, Any]:
        from agent_smith.studio import openrouter
        return await asyncio.to_thread(openrouter.account)

    @app.put("/api/openrouter/picks")
    def or_picks(body: PicksBody) -> Dict[str, Any]:
        from agent_smith.studio import openrouter
        picks = openrouter.save_picks(body.chat, body.media)
        agent.publish({"type": "models"})  # the chat's model picker reloads
        return picks

    # ---------------------------------------------------------------- attachments and voice
    @app.post("/api/uploads")
    async def upload(request: Request) -> Dict[str, Any]:
        """Raw body (no multipart dependency); the file name comes URL-encoded in X-Filename."""
        from urllib.parse import unquote
        size = int(request.headers.get("content-length") or 0)
        if size > MAX_UPLOAD:
            raise WorkspaceError(413, f"file too large (max {MAX_UPLOAD // 1024 // 1024} MB)")
        data = await request.body()
        return agent.media.save_upload(unquote(request.headers.get("x-filename") or "file"),
                                       (request.headers.get("content-type") or "").split(";")[0], data)

    @app.get("/api/uploads/{uid}")
    def upload_file(uid: str) -> Response:
        info = agent.media.get(uid)
        inline = info["kind"] in ("image", "audio", "video")  # never inline HTML/SVG: no script from an upload
        return FileResponse(agent.media.file(uid), media_type=info["mime"] if inline else "application/octet-stream",
                            headers={"Content-Disposition": ("inline" if inline else "attachment") + f'; filename="{info["name"]}"',
                                     "Content-Security-Policy": "sandbox; default-src 'none'"})

    @app.post("/api/voice")
    async def voice(request: Request) -> Dict[str, Any]:
        """A recording from the browser (WAV) -> text, by the transcribe model (config/models.json)."""
        data = await request.body()
        fmt = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3"}.get(
            (request.headers.get("content-type") or "audio/wav").split(";")[0], "wav")
        tier = "free" if request.headers.get("x-tier") == "free" else "paid"  # the chat's tier: free never uses OpenRouter
        text = await asyncio.to_thread(agent.media.transcribe, data, fmt, request.headers.get("x-language") or "", tier)
        return {"text": text}

    # ---------------------------------------------------------------- apps the agent built and runs (run_app)
    @app.get("/api/apps")
    def apps_list() -> Dict[str, Any]:
        return {"apps": agent.apps.list()}

    @app.get("/api/apps/{app_id}/logs")
    def apps_logs(app_id: str, lines: int = 300) -> Dict[str, Any]:
        return {"logs": agent.apps.logs_by_id(app_id, max(1, min(lines, 2000)))}

    @app.post("/api/apps/{app_id}/stop")
    def apps_stop(app_id: str) -> Dict[str, Any]:
        return agent.apps.stop_by_id(app_id)

    # ---------------------------------------------------------------- Agent Smith runs started from the chat
    @app.get("/api/scenarios")
    def scenarios_list() -> Dict[str, Any]:
        return {"available": agent.scenarios.unavailable() is None, "reason": agent.scenarios.unavailable(),
                "jobs": agent.scenarios.list()}

    @app.get("/api/scenarios/{job_id}/trace")
    def scenario_trace(job_id: str, since: int = 0) -> Dict[str, Any]:
        try:
            return agent.scenarios.trace(job_id, max(0, since))
        except KeyError:
            raise WorkspaceError(404, "unknown run") from None

    # ---------------------------------------------------------------- agent chat
    @app.get("/api/agent/models")
    def agent_models() -> Dict[str, Any]:
        return agent.models(on_catalog=lambda: agent.publish({"type": "models"}))

    @app.get("/api/agent/state")
    def agent_state() -> Dict[str, Any]:
        return agent.state()

    @app.post("/api/agent/runs")
    def agent_start(body: AskBody) -> Dict[str, Any]:
        return agent.start(body.message, body.mode, body.provider, body.model, body.attachments).public()

    @app.post("/api/agent/runs/{run_id}/stop")
    def agent_stop(run_id: str) -> Dict[str, Any]:
        agent.get(run_id)
        agent.stop(run_id)
        return {"ok": True}

    @app.get("/api/agent/runs/{run_id}/diff")
    def agent_diff(run_id: str, path: str) -> Dict[str, Any]:
        return agent.diff(run_id, path)

    @app.post("/api/agent/runs/{run_id}/undo")
    def agent_undo(run_id: str, body: UndoBody) -> Dict[str, Any]:
        return agent.undo(run_id, body.paths)

    @app.get("/api/agent/runs/{run_id}/trace", response_class=PlainTextResponse)
    def agent_trace(run_id: str) -> str:
        return agent.trace(run_id)

    @app.post("/api/agent/clear")
    def agent_clear() -> Dict[str, Any]:
        agent.clear()
        return {"ok": True}

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

    # ---------------------------------------------------------------- "Inside the agent" (the observatory UI)
    @app.get("/observatory", response_class=HTMLResponse)
    def observatory() -> str:  # static page, no data in it: the studio posts the trace into it
        page = (PROJECT_ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
                "content='width=device-width, initial-scale=1'><meta name='referrer' content='no-referrer'></head><body>"
                + page + "</body></html>")

    # ---------------------------------------------------------------- web app
    if web_dist is not None and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def not_built() -> str:
            return NOT_BUILT

    app.add_middleware(GuardMiddleware, token=token)
    return app
