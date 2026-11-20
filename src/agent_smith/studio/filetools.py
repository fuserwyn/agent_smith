"""write_file / append_file: whole files written in one or several steps (studio, Agent mode, in-process).

edit_file needs an existing anchor; a model with a small output limit (e.g. a free tier capped at ~1k tokens
per reply) cannot put a long file in one step either. These two let it write the first part, then append the
next ones. Paths are jailed to the workspace like every studio file operation.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from agent_smith.mcp_client.client import ToolOutcome, ToolParam, ToolSpec
from agent_smith.studio.workspace import Workspace, WorkspaceError

TOOLS = ("write_file", "append_file")
MAX_CHARS = 2_000_000


def specs() -> list:
    p = lambda n, d: ToolParam(n, "str", d, required=True)  # noqa: E731
    return [
        ToolSpec("write_file", "Create or overwrite a file with this content (parent folders are created). For a long file, "
                 "write the first part here and add the rest with append_file in the next steps.",
                 [p("path", "file path in the project"), p("content", "the text to write")]),
        ToolSpec("append_file", "Add text at the end of a file (created if missing) — to write a long file in several steps.",
                 [p("path", "file path in the project"), p("content", "the text to add")]),
    ]


class FileTools:
    def __init__(self, ws: Workspace):
        self.ws = ws

    def _rel(self, path: str) -> str:
        rel = str(path or "").strip()
        for prefix in ("/workspace/", str(self.ws.root).rstrip("/") + "/"):  # container path / host path
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
        return rel.lstrip("./") if rel.startswith("./") else rel

    def call(self, name: str, args: Dict[str, Any]) -> ToolOutcome:
        request = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name, "arguments": {**args, "content": f"<{len(str(args.get('content', '')))} chars>"}}}
        t0 = time.monotonic()
        try:
            content = args.get("content")
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            if len(content) > MAX_CHARS:
                raise ValueError("content too large")
            rel = self._rel(args.get("path", ""))
            path = self.ws.resolve(rel, must_exist=False)
            if path.is_dir():
                raise ValueError(f"{rel} is a folder")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a" if name == "append_file" else "w", encoding="utf-8") as fh:
                fh.write(content)
            size = path.stat().st_size
            text, error = (f"{'appended' if name == 'append_file' else 'wrote'} {len(content)} chars to {rel} "
                           f"(file is now {size} bytes, {path.read_text(encoding='utf-8', errors='replace').count(chr(10))} lines)"), False
        except (WorkspaceError, ValueError, OSError) as exc:
            text, error = f"{name} failed: {exc}", True
        return ToolOutcome(text, error, request=request, transport="in-process (Agent Smith Studio)",
                           rtt_ms=round((time.monotonic() - t0) * 1000, 1))
