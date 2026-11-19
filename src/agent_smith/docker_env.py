"""Docker container lifecycle for SWE-bench tasks.

The container is started with ``--network none`` (the agent must not reach the
internet: AI-safety requirement) and labelled ``agent-smith=1`` so leftovers can
always be found and removed. ``close()`` is idempotent and is also registered
with ``atexit`` so the container is removed even on unexpected exits.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import subprocess
import time
from typing import Optional

log = logging.getLogger(__name__)
LABEL = "agent-smith=1"


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
    """``swebench/x:tag`` -> ``docker.io/swebench/x:tag``; ``python:3.11`` -> ``docker.io/library/python:3.11``.

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
        args = ["run", "-d", "--name", self.name, "--label", LABEL, "--network", self.network]
        if self.platform:
            args += ["--platform", self.platform]
        args += [self.image, "tail", "-f", "/dev/null"]
        self.container_id = _docker(*args, timeout=300).stdout.strip()
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
