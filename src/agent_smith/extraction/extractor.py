"""Extract executable Python from an LLM response.

Supported formats (tried in this order):

1. Python code blocks (primary):  ```python ... ```  optionally followed by <end_code>
2. <code> ... </code> tags
3. XML tool calls (Anthropic style): <invoke name="t"><parameter name="p">v</parameter></invoke>
4. JSON / Hermes tool calls:        <tool_call>{"name": "t", "arguments": {...}}</tool_call>
5. ReAct:                           Action: t  /  Action Input: {...}

Non-Python formats are converted to equivalent Python calls, so the sandbox
stays format-agnostic. Every repair is recorded in ``notes`` and reported to
the LLM ("malformed but interpreted anyway — here is how").
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

END_CODE = "<end_code>"
STOP_SEQUENCES = [END_CODE, "\nObservation:", "</tool_call>", "</function_calls>"]

_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+\-]*)[ \t]*\n(.*?)```", re.S)
_OPEN_FENCE_RE = re.compile(r"```[ \t]*(python|py|python3)?[ \t]*\n(.*)$", re.S | re.I)
_CODE_TAG_RE = re.compile(r"<code>\s*(.*?)\s*(?:</code>|$)", re.S)
_INVOKE_RE = re.compile(r"<invoke\s+name=[\"']([\w.]+)[\"']\s*>(.*?)(?:</invoke>|$)", re.S)
_PARAM_RE = re.compile(r"<parameter\s+name=[\"'](\w+)[\"']\s*>(.*?)</parameter>", re.S)
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)
_REACT_RE = re.compile(r"Action:\s*([\w.]+)\s*\n\s*Action Input:\s*(.*?)(?=\n\s*(?:Observation|Thought|Action):|\Z)", re.S)
_PY_LANGS = {"python", "py", "python3", "py3"}


@dataclass
class Extraction:
    code: Optional[str]
    format: str = "none"
    notes: List[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.code and self.code.strip())


def _parses(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _clean(code: str, notes: List[str]) -> str:
    code = code.replace(END_CODE, "").replace("\r\n", "\n")
    lines = code.split("\n")
    if any(l.startswith(">>> ") for l in lines):
        code = "\n".join(l[4:] if l.startswith((">>> ", "... ")) else l for l in lines
                         if l.startswith((">>> ", "... ")))
        notes.append("Interactive-prompt markers ('>>> ', '... ') were stripped from your code.")
    if not _parses(code):
        dedented = textwrap.dedent(code)
        if dedented != code and _parses(dedented):
            notes.append("Your code was uniformly indented; it was dedented before execution.")
            code = dedented
    return code.strip("\n")


def _py_literal(value: Any) -> str:
    return repr(value)


def _call_code(name: str, args: Dict[str, Any]) -> str:
    """Convert a structured tool call into Python source."""
    if name == "final_answer":
        value = args.get("answer", next(iter(args.values()), "")) if args else ""
        return f"final_answer({_py_literal(value)})"
    rendered = ", ".join(f"{k}={_py_literal(v)}" for k, v in args.items() if k.isidentifier())
    return f"result = {name}({rendered})\nprint(result)"


def _coerce_param(raw: str) -> Any:
    text = raw.strip()
    if text and text[0] in "[{\"" or text in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", text):
        try:
            return json.loads(text)
        except ValueError:
            pass
    return raw.strip("\n")


def _parse_json_args(raw: str) -> Optional[Dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        try:
            value = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
    if isinstance(value, dict):
        return value
    return {"input": value}


def extract_code(text: str, known_tools: Iterable[str] = ()) -> Extraction:
    """Return the code to execute (or ``code=None`` when nothing usable was found)."""
    known = set(known_tools) | {"final_answer"}
    notes: List[str] = []
    text = text or ""

    # 1. fenced blocks
    blocks = [(lang.lower(), body) for lang, body in _FENCE_RE.findall(text)]
    py_blocks = [b for lang, b in blocks if lang in _PY_LANGS]
    if not py_blocks:
        untagged = [b for lang, b in blocks if lang in ("", "text", "code") and _parses(textwrap.dedent(b))
                    and b.strip()]
        if untagged:
            py_blocks = untagged
            notes.append("Code block had no 'python' language tag; it was interpreted as Python.")
    if py_blocks:
        if len(py_blocks) > 1:
            notes.append(f"{len(py_blocks)} code blocks were found; they were executed sequentially as one program.")
        return Extraction(_clean("\n\n".join(py_blocks), notes), "python_block", notes)

    m = _OPEN_FENCE_RE.search(text)
    if m and m.group(2).strip():
        notes.append("Your ```python block was not closed with ```; everything after the opening fence "
                     "was interpreted as code.")
        return Extraction(_clean(m.group(2).replace("```", ""), notes), "python_block_unclosed", notes)

    # 2. <code> tags
    tags = [b for b in _CODE_TAG_RE.findall(text) if b.strip()]
    if tags:
        return Extraction(_clean("\n\n".join(tags), notes), "code_tag", notes)

    # 3. XML invoke
    invokes = _INVOKE_RE.findall(text)
    if invokes:
        calls = []
        for name, body in invokes:
            params = {k: _coerce_param(v) for k, v in _PARAM_RE.findall(body)}
            calls.append(_call_code(name, params))
        notes.append("XML tool call(s) were converted to Python: " + " | ".join(c.split("\n")[0] for c in calls))
        return Extraction("\n".join(calls), "xml_invoke", notes)

    # 4. Hermes / JSON tool calls
    raw_calls = _TOOL_CALL_RE.findall(text)
    if raw_calls:
        calls = []
        for raw in raw_calls:
            try:
                obj = json.loads(raw)
            except ValueError:
                obj = None
            if not isinstance(obj, dict) or "name" not in obj:
                notes.append(f"Ignored an unparseable <tool_call> payload: {raw[:120]!r}")
                continue
            args = obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(args, str):
                args = _parse_json_args(args) or {}
            calls.append(_call_code(str(obj["name"]), args))
        if calls:
            notes.append("JSON tool call(s) were converted to Python: " + " | ".join(c.split("\n")[0] for c in calls))
            return Extraction("\n".join(calls), "hermes_tool_call", notes)

    # 5. ReAct
    react = _REACT_RE.findall(text)
    if react:
        calls = []
        for name, raw in react:
            args = _parse_json_args(raw)
            if args is None:
                args = {"input": raw.strip()}
                notes.append(f"Action Input for {name} was not valid JSON; passed as a single string.")
            if name not in known and known - {"final_answer"}:
                notes.append(f"Action '{name}' is not a known tool; it was called anyway.")
            calls.append(_call_code(name, args))
        notes.append("ReAct action(s) were converted to Python: " + " | ".join(c.split("\n")[0] for c in calls))
        return Extraction("\n".join(calls), "react", notes)

    return Extraction(None, "none", notes)


NO_CODE_FEEDBACK = (
    "[ERROR] No valid code block was found in your response, so nothing was executed.\n"
    "Respond with a short Thought, then ONE code block in exactly this format:\n"
    "```python\n# your code, e.g.\nresult = some_tool(arg)\nprint(result)\n```<end_code>\n"
    "To finish, call final_answer(...) inside a code block."
)
