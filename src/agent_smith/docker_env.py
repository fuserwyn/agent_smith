"""Docker container lifecycle for SWE-bench tasks.

The container is started with ``--network none`` (the agent must not reach the
internet: AI-safety requirement) and labelled ``agent-smith=1`` so leftovers can
always be found and removed. ``close()`` is idempotent and is also registered
with ``atexit`` so the container is removed even on unexpected exits. For the exits
nothing can catch (``kill -9``), a separate reaper process (``container_reaper.py``)
removes the container once the agent's pid is gone, and every new run removes the
containers of agents that are no longer alive (``agent-smith.pid`` label).
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)
LABEL = "agent-smith=1"
PID_LABEL = "agent-smith.pid"
REAPER = Path(__file__).with_name("container_reaper.py")


class DockerError(RuntimeError):
    pass


def _docker(*args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise DockerError("docker CLI not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerError(f"docker {args[0]} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise DockerError(f"docker {' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
    return proc


def qualify_image(image: str) -> str:
    """``ns/x:tag`` -> ``docker.io/ns/x:tag``; ``python:3.11`` -> ``docker.io/library/python:3.11``.

    Podman (Fedora, 42 computers) refuses short names without a TTY ("short-name resolution enforced");
    Docker accepts the full name too, so it is always used."""
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return image  # already has a registry
    return f"docker.io/{image}" if rest else f"docker.io/library/{image}"


class DockerEnv:
    def __init__(self, image: str, instance_id: str, network: str = "none", platform: Optional[str] = None):
        self.image = qualify_image(image)
        self.instance_id = instance_id
        self.network = network
        # SWE-bench images are x86_64 only; on Apple Silicon Docker runs them under emulation.
        self.platform = platform or os.environ.get("SWEBENCH_DOCKER_PLATFORM") or (
            "linux/amd64" if "x86_64" in image else None)
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", instance_id)[:60]
        self.name = f"agent-smith-{safe}-{os.getpid()}-{int(time.time())}"
        self.container_id: Optional[str] = None
        atexit.register(self.close)

    def ensure_image(self, timeout: float = 3600) -> None:
        if _docker("image", "inspect", self.image, check=False, timeout=60).returncode == 0:
            return
        log.info("pulling %s (this can take a while)", self.image)
        args = ["pull"] + (["--platform", self.platform] if self.platform else []) + [self.image]
        _docker(*args, timeout=timeout)

    def start(self) -> str:
        cleanup_orphans()
        args = ["run", "-d", "--name", self.name, "--label", LABEL, "--label", f"{PID_LABEL}={os.getpid()}",
                "--network", self.network]
        if self.platform:
            args += ["--platform", self.platform]
        args += [self.image, "tail", "-f", "/dev/null"]
        self.container_id = _docker(*args, timeout=300).stdout.strip()
        start_reaper(self.name)
        return self.name

    def close(self) -> None:
        if not self.container_id:
            return
        try:
            _docker("rm", "-f", self.name, timeout=60, check=False)
            log.info("removed container %s", self.name)
        except DockerError as exc:
            log.warning("could not remove container %s: %s", self.name, exc)
        self.container_id = None

    def __enter__(self) -> "DockerEnv":
        self.ensure_image()
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def cleanup_leftovers() -> int:
    """Remove every container carrying our label (used by tests / after crashes)."""
    ids = _docker("ps", "-aq", "--filter", f"label={LABEL}", check=False).stdout.split()
    for cid in ids:
        _docker("rm", "-f", cid, check=False)
    return len(ids)


def start_reaper(name: str) -> None:
    """Watch this process from outside: if it dies without cleaning up (kill -9), remove the container."""
    try:
        subprocess.Popen([sys.executable, str(REAPER), str(os.getpid()), name], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError as exc:  # the atexit cleanup still covers normal exits
        log.warning("could not start the container reaper: %s", exc)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_orphans() -> int:
    """Remove our containers whose agent process is gone (left by a crash before the reaper could act)."""
    out = _docker("ps", "-a", "--filter", f"label={LABEL}", "--format", f'{{{{.Names}}}} {{{{.Label "{PID_LABEL}"}}}}',
                  check=False, timeout=60).stdout
    removed = 0
    for line in out.splitlines():
        name, _, pid = line.partition(" ")
        if pid.strip().isdigit() and not _alive(int(pid)):
            _docker("rm", "-f", name, check=False, timeout=60)
            removed += 1
    return removed
