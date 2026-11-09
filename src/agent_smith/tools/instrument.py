"""Wrap plain tool functions so MCP results carry an execution report in ``_meta``.

The text content is exactly what the tool returned (clients that ignore
``_meta`` see no difference). Under ``_meta["agent_smith"]`` the server reports:
the arguments it received, the Python handler it called, every operation that
touched the repository (command, exit code, duration, bytes) and the size of
the raw vs returned output. The UI uses it to show each hop of a tool call.
"""

from __future__ import annotations

import functools
import inspect
import time
import typing
from typing import Any, Callable, Dict, List

from mcp.types import CallToolResult, TextContent


def instrumented(server_name: str, handler: str, get_ops: Callable[[], List[Dict[str, Any]]],
                 reset_ops: Callable[[], None], executor_info: Callable[[], Dict[str, Any]],
                 get_details: Callable[[], Dict[str, Any]] = dict):
    def decorate(fn: Callable[..., str]):
        # Resolve string annotations (``from __future__ import annotations``) in the tool's own module.
        hints = typing.get_type_hints(fn)
        sig = inspect.signature(fn)
        sig = sig.replace(parameters=[p.replace(annotation=hints.get(p.name, p.annotation))
                                      for p in sig.parameters.values()])

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            reset_ops()
            t0 = time.monotonic()
            error = None
            try:
                text = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - reported as an MCP error result
                text, error = f"{type(exc).__name__}: {exc}", str(exc)
            ops = list(get_ops())
            raw = sum(op.get("stdout_bytes", 0) for op in ops)
            meta = {
                "server": server_name,
                "tool": fn.__name__,
                "handler": f"{handler}.{fn.__name__}",
                "received_args": {k: v for k, v in bound.arguments.items()},
                "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                "ops": ops,
                "raw_output_bytes": raw,
                "result_chars": len(text),
                "result_lines": text.count("\n") + 1 if text else 0,
                "truncated": "truncated" in text,
                **executor_info(),
            }
            details = get_details()
            if details:
                meta["details"] = details  # where the tool's inputs came from (tests, scripts...)
            return CallToolResult.model_validate({
                "content": [TextContent(type="text", text=text)],
                "isError": error is not None,
                "_meta": {"agent_smith": meta},
            })

        wrapper.__signature__ = sig.replace(return_annotation=CallToolResult)
        wrapper.__annotations__ = {**hints, "return": CallToolResult}
        return wrapper

    return decorate
