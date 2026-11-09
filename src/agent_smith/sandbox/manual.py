"""Generate the "sandbox manual" fed to the LLM.

The manual is built dynamically from the connected MCP server's tool schemas,
so connecting a different server automatically changes what the LLM reads.
"""

from __future__ import annotations

import textwrap
from typing import Optional

from agent_smith.models import SandboxConfig


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_manual(config: SandboxConfig, mcp=None, compact: bool = False) -> str:
    """Return the manual text.

    ``compact=True`` produces a terse version for tight token budgets (MBPP).
    """
    lines = ["## Tools (Python functions available in the sandbox; each returns a str)"]
    desc_limit = 160 if compact else 600
    for tool in (mcp.tools if mcp else []):
        lines.append(f"- {tool.signature()}")
        if tool.description:
            lines.append(textwrap.indent(_shorten(tool.description, desc_limit), "    "))
        if not compact:
            for p in tool.params:
                if p.description:
                    lines.append(f"      {p.name}: {_shorten(p.description, 200)}")
    lines.append("- final_answer(answer: str) -> None\n    Built-in. Submits your final answer and ends the task.")
    if mcp and mcp.has_resources:
        lines.append("- list_resources() / read_resource(uri) -> str   (MCP resources)")
    if mcp and mcp.has_prompts:
        lines.append("- list_prompts() / get_prompt(name, arguments: dict) -> str   (MCP prompts)")
    lines.append("Tool errors raise ToolError (catchable).")

    rules = [
        "## Sandbox rules",
        f"- Allowed imports ONLY: {', '.join(config.authorized_imports)}",
        f"- Files: only under {', '.join(config.allowed_directories)} (use tools for the repository)",
        "- No network, no subprocesses, no eval/exec/open of other paths, no dunder attributes.",
        f"- Each code block: {config.max_execution_time_seconds}s timeout, {config.max_memory_mb} MB memory.",
        "- Variables persist between steps. Only print()ed output and the last expression are shown.",
    ]
    return "\n".join(lines + [""] + rules)


def describe_connection(mcp: Optional[object]) -> str:
    if not mcp:
        return "no MCP server connected (only final_answer is available)"
    names = ", ".join(t.name for t in mcp.tools) or "(no tools)"
    return f"MCP server '{getattr(mcp, 'server_name', '')}' — tools: {names}"
