"""Where SWE-bench tools act: inside a Docker container or on the local disk.

* :class:`DockerExecutor` — ``docker exec`` into the task container
  (selected when ``SWEBENCH_CONTAINER`` is set).
* :class:`LocalExecutor` — plain subprocess / file IO under ``TESTBED_PATH``
  (used for independent tool testing without Docker).

Both expose the same tiny interface so every tool is written once. Every
operation is appended to ``ops`` (command, exit code, duration, bytes) so the
MCP server can report what really happened in the repository.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

CONDA_ACTIVATE = "source /opt/miniconda3/bin/activate testbed >/dev/null 2>&1 || true"


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def quote(s: str) -> str:
    return shlex.quote(str(s))


def _run(argv, timeout: float, stdin: Optional[str], cwd: Optional[str] = None) -> CommandResult:
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                              errors="replace")
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CommandResult(124, out, err + f"\n[command timed out after {timeout}s]", timed_out=True)
    except FileNotFoundError as exc:
        return CommandResult(127, "", str(exc))


class Executor:
    root: str
    kind: str = "base"
    target: str = ""

    def __init__(self) -> None:
        self.ops: List[Dict[str, Any]] = []

    # --- operation log -------------------------------------------------- #
    def _log(self, op: str, command: str, t0: float, exit_code: int = 0, stdout: str = "", stderr: str = "",
             stdin_bytes: int = 0, timed_out: bool = False) -> None:
        self.ops.append({
            "op": op, "command": command[:600], "exit_code": exit_code,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1),
            "stdout_bytes": len(stdout.encode()), "stderr_bytes": len(stderr.encode()), "stdin_bytes": stdin_bytes,
            "stdout_head": stdout[:300], "stderr_head": stderr[:200], "timed_out": timed_out,
        })

    def reset_ops(self) -> None:
        self.ops = []

    # --- interface ------------------------------------------------------ #
    def run(self, command: str, workdir: Optional[str] = None, timeout: float = 120,
            stdin: Optional[str] = None) -> CommandResult:
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def write_text(self, path: str, content: str) -> None:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        return self.run(f"test -e {quote(path)}").exit_code == 0

    def is_dir(self, path: str) -> bool:
        return self.run(f"test -d {quote(path)}").exit_code == 0

    def resolve(self, path: Optional[str]) -> str:
        """Absolute path; relative paths are taken from the repository root."""
        if not path:
            return self.root
        path = str(path)
        if not path.startswith("/"):
            path = os.path.join(self.root, path)
        return os.path.normpath(path)


class DockerExecutor(Executor):
    kind = "docker"

    def __init__(self, container: str, root: str = "/testbed"):
        super().__init__()
        self.container = container
        self.target = container
        self.root = root

    def run(self, command, workdir=None, timeout=120, stdin=None):
        script = f"{CONDA_ACTIVATE}; {command}"
        argv = ["docker", "exec"] + (["-i"] if stdin is not None else []) + [
            "-w", workdir or self.root, self.container, "bash", "-c", script]
        t0 = time.monotonic()
        res = _run(argv, timeout, stdin)
        shown = f"docker exec -w {workdir or self.root} {self.container} bash -c {quote(command)}"
        self._log("run", shown, t0, res.exit_code, res.stdout, res.stderr, len((stdin or "").encode()), res.timed_out)
        return res

    def read_text(self, path):
        t0 = time.monotonic()
        res = _run(["docker", "exec", self.container, "cat", path], 60, None)
        self._log("read", f"docker exec {self.container} cat {path}", t0, res.exit_code, res.stdout, res.stderr)
        if res.exit_code != 0:
            raise FileNotFoundError(res.stderr.strip() or f"cannot read {path}")
        return res.stdout

    def write_text(self, path, content):
        t0 = time.monotonic()
        res = _run(["docker", "exec", "-i", self.container, "bash", "-c",
                    f'mkdir -p "$(dirname {quote(path)})" && cat > {quote(path)}'], 60, content)
        self._log("write", f"docker exec -i {self.container} bash -c 'cat > {path}'  < {len(content.encode())} bytes",
                  t0, res.exit_code, res.stdout, res.stderr, len(content.encode()))
        if res.exit_code != 0:
            raise OSError(res.stderr.strip() or f"cannot write {path}")


class LocalExecutor(Executor):
    kind = "local"

    def __init__(self, root: str):
        super().__init__()
        self.root = os.path.abspath(root)
        self.target = self.root

    def run(self, command, workdir=None, timeout=120, stdin=None):
        t0 = time.monotonic()
        res = _run(["bash", "-c", command], timeout, stdin, cwd=workdir or self.root)
        self._log("run", f"(cd {workdir or self.root} && bash -c {quote(command)})", t0, res.exit_code, res.stdout,
                  res.stderr, len((stdin or "").encode()), res.timed_out)
        return res

    def read_text(self, path):
        t0 = time.monotonic()
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            self._log("read", f"open({path!r}).read()", t0, 1, "", str(exc))
            raise
        self._log("read", f"open({path!r}).read()", t0, 0, text)
        return text

    def write_text(self, path, content):
        t0 = time.monotonic()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        self._log("write", f"open({path!r}, 'w').write(<{len(content.encode())} bytes>)", t0, 0,
                  stdin_bytes=len(content.encode()))

    def exists(self, path):
        return os.path.exists(path)

    def is_dir(self, path):
        return os.path.isdir(path)


def executor_from_env() -> Executor:
    container = os.environ.get("SWEBENCH_CONTAINER")
    root = os.environ.get("TESTBED_PATH", "/testbed")
    if container:
        return DockerExecutor(container, root)
    return LocalExecutor(root)
