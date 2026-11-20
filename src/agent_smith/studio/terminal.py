"""Terminal sessions for the studio: a real shell behind a PTY, streamed over a WebSocket.

Isolation: by default the shell runs **inside a Docker container of the project** (``docker exec`` into
a long-lived ``agent-studio-<id>`` container that mounts only the project folder at /workspace).
A shell directly on the host is possible only with ``--shell local`` (the CLI warns about it).

Sessions outlive the WebSocket: reloading the page re-attaches and replays the recent output
(scrollback). The container and every shell are cleaned up when the server stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import pty
import shutil
import signal
import struct
import subprocess
import termios
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

LABEL = "agent-smith-studio"
SCROLLBACK = 256 * 1024
MAX_SESSIONS = 8
DEFAULT_IMAGE = "python:3.11-slim"


class TerminalError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


@dataclass
class ProjectContainer:
    """One long-lived container per project; shells are ``docker exec`` sessions into it."""

    root: Path
    image: str = DEFAULT_IMAGE
    network: str = "bridge"  # the terminal is for the user: pip install etc. need the network
    started: bool = False

    @property
    def name(self) -> str:
        return "agent-studio-" + hashlib.sha1(str(self.root).encode()).hexdigest()[:10]

    def available(self) -> Optional[str]:
        """None when Docker can be used, otherwise a human-readable reason."""
        if not shutil.which("docker"):
            return "Docker is not installed"
        try:
            if _docker("info", "--format", "{{.ServerVersion}}", timeout=6).returncode != 0:
                return "Docker is not running"
        except (OSError, subprocess.TimeoutExpired):
            return "Docker does not answer"
        return None

    def running(self) -> bool:
        r = _docker("inspect", "-f", "{{.State.Running}}", self.name, timeout=15)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def ensure(self) -> None:
        if self.running():
            self.started = True
            return
        _docker("rm", "-f", self.name, timeout=30)  # a stopped leftover with the same name
        user = [] if os.name != "posix" else ["--user", f"{os.getuid()}:{os.getgid()}"]  # files keep your ownership
        r = _docker("run", "-d", "--name", self.name, "--label", f"{LABEL}=1", "--network", self.network,
                    "-v", f"{self.root}:/workspace", "-w", "/workspace", *user,
                    "-e", "HOME=/tmp", "-e", "TERM=xterm-256color",
                    "--init", self.image, "sleep", "infinity", timeout=600)
        if r.returncode != 0:
            raise TerminalError(503, f"could not start the project container: {r.stderr.strip()[:300]}")
        self.started = True

    def shell_argv(self) -> List[str]:
        return ["docker", "exec", "-it", "-e", "TERM=xterm-256color", "-e", "PS1=\\[\\e[38;5;111m\\]\\w\\[\\e[0m\\] $ ",
                self.name, "bash", "--noprofile", "--norc", "-i"]

    def remove(self) -> None:
        with contextlib.suppress(Exception):
            _docker("rm", "-f", self.name, timeout=30)


@dataclass
class Session:
    id: str
    title: str
    mode: str
    pid: int
    fd: int
    started: float = field(default_factory=time.time)
    buffer: Deque[bytes] = field(default_factory=deque)
    size: int = 0
    exit_code: Optional[int] = None
    listeners: Set[asyncio.Queue] = field(default_factory=set)

    def remember(self, data: bytes) -> None:
        self.buffer.append(data)
        self.size += len(data)
        while self.size > SCROLLBACK and len(self.buffer) > 1:
            self.size -= len(self.buffer.popleft())

    def info(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "mode": self.mode, "alive": self.exit_code is None,
                "exit_code": self.exit_code, "started": self.started}


class TerminalManager:
    def __init__(self, root: Path, mode: str = "docker", image: str = DEFAULT_IMAGE):
        if mode not in ("docker", "local"):
            raise ValueError("mode must be docker or local")
        self.root, self.mode = Path(root).resolve(), mode
        self.container = ProjectContainer(self.root, image=image)
        self.sessions: Dict[str, Session] = {}
        self._seq = 0
        import atexit
        atexit.register(self.close_all)  # also on Ctrl+C / crash: no shell or container left behind

    def status(self) -> Dict[str, Any]:
        reason = self.container.available() if self.mode == "docker" else None
        return {"mode": self.mode, "available": reason is None, "reason": reason,
                "image": self.container.image if self.mode == "docker" else None,
                "container": self.container.name if self.mode == "docker" else None,
                "sessions": [s.info() for s in self.sessions.values()]}

    # ------------------------------------------------------------------ lifecycle
    async def create(self, cols: int = 100, rows: int = 30) -> Session:
        if sum(1 for s in self.sessions.values() if s.exit_code is None) >= MAX_SESSIONS:
            raise TerminalError(429, f"at most {MAX_SESSIONS} terminals")
        if self.mode == "docker":
            reason = self.container.available()
            if reason:
                raise TerminalError(503, reason)
            await asyncio.to_thread(self.container.ensure)
            argv = self.container.shell_argv()
        else:
            shell = os.environ.get("SHELL") or "/bin/bash"
            argv = [shell, "-i"]
        pid, fd = self._spawn(argv, cols, rows)
        self._seq += 1
        s = Session(id=uuid.uuid4().hex[:12], title=f"{'bash' if self.mode == 'docker' else Path(argv[0]).name} {self._seq}",
                    mode=self.mode, pid=pid, fd=fd)
        self.sessions[s.id] = s
        asyncio.get_running_loop().add_reader(fd, self._on_output, s)
        return s

    def _spawn(self, argv: List[str], cols: int, rows: int) -> "tuple[int, int]":
        env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        for k in [k for k in env if k.endswith(("_API_KEY", "_API_KEYS", "_TOKEN"))] + ["STUDIO_TOKEN"]:
            env.pop(k, None)  # the session key and provider keys never reach the shell
        pid, fd = pty.fork()
        if pid == 0:  # child: becomes the shell (stdin/out/err = the PTY)
            try:
                os.chdir(self.root)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        self._winsize(fd, cols, rows)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return pid, fd

    @staticmethod
    def _winsize(fd: int, cols: int, rows: int) -> None:
        cols, rows = max(2, min(int(cols), 1000)), max(2, min(int(rows), 500))
        with contextlib.suppress(OSError):
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _on_output(self, s: Session) -> None:
        try:
            data = os.read(s.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            s.remember(data)
            for q in list(s.listeners):
                q.put_nowait(data)
            return
        # EOF: the shell exited
        asyncio.get_running_loop().remove_reader(s.fd)
        with contextlib.suppress(OSError):
            os.close(s.fd)
        s.exit_code = self._reap(s.pid)
        for q in list(s.listeners):
            q.put_nowait(None)

    @staticmethod
    def _reap(pid: int, wait: float = 1.0) -> int:
        deadline = time.monotonic() + wait
        while True:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return 0
            if done:
                return os.waitstatus_to_exitcode(status)
            if time.monotonic() > deadline:
                return -1
            time.sleep(0.02)

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if not s:
            raise TerminalError(404, "no such terminal")
        return s

    def write(self, sid: str, data: str) -> None:
        s = self.get(sid)
        if s.exit_code is None:
            with contextlib.suppress(OSError):
                os.write(s.fd, data.encode("utf-8", errors="replace"))

    def resize(self, sid: str, cols: int, rows: int) -> None:
        s = self.get(sid)
        if s.exit_code is None:
            self._winsize(s.fd, cols, rows)

    def kill(self, sid: str) -> None:
        s = self.sessions.pop(sid, None)
        if not s:
            return
        if s.exit_code is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(s.pid), signal.SIGHUP)
            with contextlib.suppress(OSError, ValueError, RuntimeError):  # RuntimeError: no loop (atexit)
                asyncio.get_running_loop().remove_reader(s.fd)
            with contextlib.suppress(OSError):
                os.close(s.fd)
            self._reap(s.pid, wait=0.5)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(s.pid, signal.SIGKILL)
            self._reap(s.pid, wait=0.2)
        for q in list(s.listeners):
            q.put_nowait(None)

    def close_all(self) -> None:
        for sid in list(self.sessions):
            self.kill(sid)
        if self.mode == "docker" and self.container.started:
            self.container.remove()
            self.container.started = False
