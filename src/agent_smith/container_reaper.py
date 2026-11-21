"""Removes a task container when the agent process dies, even by ``kill -9`` (no atexit, no signal handler runs).

Started by ``DockerEnv.start`` as a separate process in its own session (a Ctrl+C on the agent's terminal does not
reach it), standard library only:

    python container_reaper.py <agent pid> <container name>

It polls the agent's pid twice a second; once the agent is gone it runs ``docker rm -f <container>`` and exits.
When the agent removes the container itself (normal exit, Ctrl+C, SIGTERM) the ``rm`` is a harmless no-op.
"""

import os
import subprocess
import sys
import time


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def main() -> int:
    pid, name = int(sys.argv[1]), sys.argv[2]
    while alive(pid):
        time.sleep(0.5)
    subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    return 0


if __name__ == "__main__":
    sys.exit(main())
