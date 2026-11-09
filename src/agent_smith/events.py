"""Structured event trace of an agent run (JSON lines).

Each event: ``{"t": <seconds since run start>, "ts": ISO time, "type": ..., ...data}``.
The trace is consumed by the interactive UI (``ui/index.html`` / ``uv run agent-ui``)
and is handy to debug the first iterations of a run.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

EventCallback = Callable[[str, Dict[str, Any]], None]


class EventSink:
    def __init__(self, path: Optional[str] = None, listeners: Optional[List[EventCallback]] = None):
        self.path = path
        self.listeners = list(listeners or [])
        self.start = time.monotonic()
        self._lock = threading.Lock()
        self._fh = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def emit(self, kind: str, data: Optional[Dict[str, Any]] = None) -> None:
        record = {"t": round(time.monotonic() - self.start, 3), "ts": datetime.now().isoformat(),
                  "type": kind, **(data or {})}
        with self._lock:
            if self._fh:
                self._fh.write(json.dumps(record, default=str) + "\n")
                self._fh.flush()
        for listener in self.listeners:
            try:
                listener(kind, record)
            except Exception:  # noqa: BLE001 - observers must never break the agent
                pass

    __call__ = emit

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


NULL_SINK = EventSink()
