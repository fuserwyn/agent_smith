"""Synchronous MCP client (stdio or streamable HTTP).

The MCP SDK is asyncio-based while the sandbox host is synchronous, so the
session lives in a private event loop running on a background thread. Every
public method schedules a coroutine on that loop and waits for the result.

Nothing here hard-codes tool names: tools, resources and prompts are
discovered dynamically from whatever server is connected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class ToolParam:
    name: str
    type: str = "Any"
    description: str = ""
    required: bool = False
    default: Any = None
    has_default: bool = False


@dataclass
class ToolSpec:
    """Tool description derived from the server's JSON schema."""

    name: str
    description: str = ""
    params: List[ToolParam] = field(default_factory=list)

    def signature(self) -> str:
        parts = []
        for p in self.params:
            text = f"{p.name}: {p.type}"
            if not p.required:
                text += f" = {p.default!r}" if p.has_default else " = None"
            parts.append(text)
        return f"{self.name}({', '.join(parts)}) -> str"

    def to_worker(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "doc": self.description,
            "params": [{"name": p.name, "required": p.required} for p in self.params],
        }


@dataclass
class ToolOutcome:
    text: str
    is_error: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)      # server execution report (_meta), if any
    request: Dict[str, Any] = field(default_factory=dict)   # JSON-RPC request sent by this client
    transport: str = ""
    rtt_ms: float = 0.0                                     # client-side round trip


_JSON_TO_PY = {"string": "str", "integer": "int", "number": "float", "boolean": "bool",
               "array": "list", "object": "dict", "null": "None"}


def _schema_type(schema: Dict[str, Any]) -> str:
    if "type" in schema:
        t = schema["type"]
        if isinstance(t, list):
            return " | ".join(_JSON_TO_PY.get(x, x) for x in t)
        return _JSON_TO_PY.get(t, t)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return " | ".join(_schema_type(s) for s in schema[key])
    return "Any"


def tool_spec_from_schema(name: str, description: str, input_schema: Dict[str, Any]) -> ToolSpec:
    props = (input_schema or {}).get("properties", {}) or {}
    required = set((input_schema or {}).get("required", []) or [])
    params = []
    for pname, pschema in props.items():
        params.append(ToolParam(
            name=pname,
            type=_schema_type(pschema or {}),
            description=(pschema or {}).get("description", ""),
            required=pname in required,
            default=(pschema or {}).get("default"),
            has_default="default" in (pschema or {}),
        ))
    # Required parameters first so positional calls follow the documented order.
    params.sort(key=lambda p: not p.required)
    return ToolSpec(name=name, description=(description or "").strip(), params=params)


class MCPClient:
    """Connect to one MCP server and expose it synchronously."""

    def __init__(self, stdio_command: Optional[str] = None, url: Optional[str] = None,
                 env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
                 call_timeout: float = 900.0):
        if bool(stdio_command) == bool(url):
            raise ValueError("exactly one of stdio_command or url must be given")
        self.stdio_command = stdio_command
        self.url = url
        self.env = env
        self.cwd = cwd
        self.call_timeout = call_timeout
        self.tools: List[ToolSpec] = []
        self.has_resources = False
        self.has_prompts = False
        self.server_name = ""
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session = None
        self._stop: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def connect(self, timeout: float = 60.0) -> "MCPClient":
        if self.connected:
            return self
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-client", daemon=True)
        self._thread.start()
        self._runner = asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        if not self._ready.wait(timeout):
            self.close()
            raise TimeoutError("MCP server did not initialise in time")
        if self._error:
            err = self._error
            self.close()
            raise ConnectionError(f"could not connect to MCP server: {err}") from err
        return self

    async def _run(self) -> None:
        from mcp import ClientSession

        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                if self.stdio_command:
                    from mcp.client.stdio import StdioServerParameters, stdio_client

                    argv = shlex.split(self.stdio_command)
                    if argv and argv[0] in ("python", "python3"):
                        argv[0] = sys.executable  # same interpreter / venv as the agent
                    params = StdioServerParameters(command=argv[0], args=argv[1:],
                                                   env=self.env if self.env is not None else dict(os.environ),
                                                   cwd=self.cwd)
                    read, write = await stack.enter_async_context(stdio_client(params))
                else:
                    try:
                        from mcp.client.streamable_http import streamable_http_client as http_client
                    except ImportError:  # older SDKs
                        from mcp.client.streamable_http import streamablehttp_client as http_client

                    read, write, _ = await stack.enter_async_context(http_client(self.url))
                session = await stack.enter_async_context(ClientSession(read, write))
                init = await session.initialize()
                self.server_name = getattr(init.serverInfo, "name", "") or ""
                caps = init.capabilities
                self._session = session
                await self._discover(caps)
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to caller via _error
            self._error = exc
            self._ready.set()

    async def _discover(self, caps) -> None:
        result = await self._session.list_tools()
        self.tools = [tool_spec_from_schema(t.name, t.description or "", t.inputSchema or {}) for t in result.tools]
        self.has_resources = bool(getattr(caps, "resources", None))
        self.has_prompts = bool(getattr(caps, "prompts", None))

    def close(self) -> None:
        if self._loop is None:
            return
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        try:
            self._runner.result(timeout=10)
        except Exception:  # noqa: BLE001 - best effort shutdown
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        self._loop = None
        self._session = None

    @property
    def connected(self) -> bool:
        return self._loop is not None and self._session is not None

    def __enter__(self) -> "MCPClient":
        return self if self.connected else self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # calls
    # ------------------------------------------------------------------ #
    def _call(self, coro, timeout: Optional[float] = None):
        if self._loop is None or self._session is None:
            raise ConnectionError("MCP client is not connected")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout or self.call_timeout)

    def tool_names(self) -> List[str]:
        return [t.name for t in self.tools]

    @property
    def transport(self) -> str:
        return f"stdio ({self.stdio_command})" if self.stdio_command else f"streamable HTTP ({self.url})"

    def call_tool(self, name: str, args: Dict[str, Any]) -> ToolOutcome:
        self._request_id = getattr(self, "_request_id", 0) + 1
        request = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name, "arguments": args}}
        if name not in self.tool_names():
            return ToolOutcome(f"unknown tool '{name}'. Available: {', '.join(self.tool_names())}", True,
                               request=request, transport=self.transport)
        t0 = time.monotonic()
        try:
            result = self._call(self._session.call_tool(name, args))
        except Exception as exc:  # noqa: BLE001 - surfaced to the LLM as a ToolError
            return ToolOutcome(f"MCP call failed: {type(exc).__name__}: {exc}", True, request=request,
                               transport=self.transport, rtt_ms=(time.monotonic() - t0) * 1000)
        meta = (getattr(result, "meta", None) or {}).get("agent_smith", {})
        return ToolOutcome(self._content_to_text(result.content, getattr(result, "structuredContent", None)),
                           bool(result.isError), meta=meta, request=request, transport=self.transport,
                           rtt_ms=round((time.monotonic() - t0) * 1000, 1))

    @staticmethod
    def _content_to_text(content, structured=None) -> str:
        parts = []
        for block in content or []:
            btype = getattr(block, "type", "")
            if btype == "text":
                parts.append(block.text)
            elif btype == "resource":
                res = block.resource
                parts.append(getattr(res, "text", None) or f"<binary resource {res.uri}>")
            else:
                parts.append(f"<{btype} content>")
        if not parts and structured is not None:
            return json.dumps(structured, indent=2, default=str)
        return "\n".join(parts)

    def list_resources(self) -> str:
        res = self._call(self._session.list_resources())
        lines = [f"{r.uri} - {r.name}" + (f": {r.description}" if r.description else "") for r in res.resources]
        return "\n".join(lines) or "(no resources)"

    def read_resource(self, uri: str) -> str:
        from pydantic import AnyUrl

        res = self._call(self._session.read_resource(AnyUrl(uri)))
        return "\n".join(getattr(c, "text", None) or f"<binary {c.uri}>" for c in res.contents)

    def list_prompts(self) -> str:
        res = self._call(self._session.list_prompts())
        lines = []
        for p in res.prompts:
            args = ", ".join(a.name for a in (p.arguments or []))
            lines.append(f"{p.name}({args})" + (f": {p.description}" if p.description else ""))
        return "\n".join(lines) or "(no prompts)"

    def get_prompt(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        res = self._call(self._session.get_prompt(name, {k: str(v) for k, v in (arguments or {}).items()}))
        return "\n\n".join(f"[{m.role}] {getattr(m.content, 'text', m.content)}" for m in res.messages)
