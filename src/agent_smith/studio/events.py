"""Live events for the studio (``/ws/events``): file changes on disk, later agent and terminal status.

One watcher per server (watchfiles, native and debounced) runs while at least one browser tab is
connected; each tab gets its own queue so a slow tab never blocks the others.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, Dict, Optional, Set

from agent_smith.studio.workspace import HEAVY_DIRS, Workspace

KIND = {1: "added", 2: "modified", 3: "deleted"}  # watchfiles.Change values
QUEUE_MAX = 200


def _interesting(root: Path, path: str) -> bool:
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        return False
    return bool(parts) and not any(p in HEAVY_DIRS for p in parts[:-1]) and not parts[-1].startswith(".studio-")


class EventHub:
    def __init__(self, ws: Workspace):
        self.ws = ws
        self.clients: Set[asyncio.Queue] = set()
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.clients.add(q)
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._watch(self._stop))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)
        if not self.clients and self._stop is not None:
            self._stop.set()

    def publish(self, message: Dict[str, Any]) -> None:
        for q in list(self.clients):
            if q.full():  # a stuck tab: drop its oldest event, it will resync on the next change
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(message)

    async def _watch(self, stop: asyncio.Event) -> None:
        try:
            from watchfiles import awatch
        except ImportError:
            return
        root = self.ws.root
        try:
            async for batch in awatch(root, stop_event=stop, watch_filter=lambda _c, p: _interesting(root, p),
                                      debounce=150, step=50, recursive=True):
                changes = sorted({(KIND.get(int(c), "modified"), Path(p).relative_to(root).as_posix()) for c, p in batch})
                self.publish({"type": "fs", "changes": [{"kind": k, "path": p} for k, p in changes]})
        except (OSError, RuntimeError):  # watcher limits, folder removed: the UI still works with manual refresh
            self.publish({"type": "fs-watch-stopped"})

    async def close(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=2)
