"""System prompts. The tool documentation part is generated from the MCP server (see sandbox.manual)."""

from __future__ import annotations

import re
from pathlib import Path

from agent_smith.models import MBPPTaskInput, SandboxConfig, SWEBenchTaskInput
from agent_smith.sandbox.manual import build_manual

FORMAT_RULES = """Every reply has exactly this shape:
Thought: <brief reasoning>
```python
<code>
```<end_code>
Then STOP and wait: the code runs in a Python sandbox and you receive "Observation:" with its printed output.
Never write the Observation yourself. Variables persist between steps. Use print() to see results."""


def mbpp_system_prompt(config: SandboxConfig, mcp) -> str:
    tools = {t.name for t in mcp.tools} if mcp else set()
    if "run_tests" in tools:
        method = '''Your FIRST reply must already test and submit. Always use exactly this pattern
(the example solves another task: write the Thought and the function for YOUR task):
Thought: <one sentence: the idea of your solution>.
```python
import json
code = r"""
from collections import Counter
def most_common(xs):
    return Counter(xs).most_common(1)[0][0]
"""
result = json.loads(run_tests(code))
print(result["output"])
if result["success"]:
    final_answer(code)
```<end_code>
Never reply with only a function definition: it is not tested and not submitted.
If the tests fail, read got/expected, fix the code and use the same pattern again.'''
    else:
        method = "Define the function, check it with a few asserts, then call final_answer(code_string)."
    return f"""You are an expert Python programmer. You solve a programming task by writing code in a sandbox.

{FORMAT_RULES}

{build_manual(config, mcp, compact=True)}

{method}

Rules:
- `code` is a string with the complete standalone solution: needed imports + the function with the EXACT name and
  parameters requested. No prints, no tests, no input() inside it. Use only the allowed imports.
- One extra hidden test exists: implement the general behaviour described, never hard-code test values.
- final_answer receives the solution source code as a string.
- Keep the Thought to one sentence."""


def mbpp_task_prompt(task: MBPPTaskInput) -> str:
    tests = "\n".join(task.test_list) or "(none given)"
    imports = ("\nTest imports:\n" + "\n".join(task.test_imports)) if task.test_imports else ""
    return (f"Task: {task.task_definition}\n"
            f"Required signature: {task.function_definition}\n"
            f"Public tests (run_tests checks these):\n{tests}{imports}")


# SWE-bench prompt text lives in prompt_text/*.md (plain data, see prompt_text/README.md).
_TEXT_DIR = Path(__file__).with_name("prompt_text")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _text(name: str) -> str:
    return (_TEXT_DIR / f"{name}.md").read_text(encoding="utf-8").removesuffix("\n")


def _fill(template: str, **values: str) -> str:
    """One pass: ``{name}`` -> value; the values themselves are never re-scanned (task text may contain braces)."""
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), template)


SWE_METHOD = _text("swe_method")
SWE_EXAMPLE = _text("swe_example")
SWE_MINIMAL = _text("swe_minimal")


def swe_system_prompt(config: SandboxConfig, mcp, repo_root: str = "/testbed", variant: str = "full") -> str:
    """``variant="minimal"`` drops the method section and the example (used for the ablation study)."""
    guidance = f"{SWE_METHOD}\n\n{SWE_EXAMPLE}" if variant == "full" else SWE_MINIMAL
    return _fill(_text("swe_system"), repo_root=repo_root, format_rules=FORMAT_RULES,
                 manual=build_manual(config, mcp), guidance=guidance)


def swe_task_prompt(task: SWEBenchTaskInput, repo_root: str = "/testbed") -> str:
    hints = f"\n\n<hints>\n{task.hints_text.strip()}\n</hints>" if task.hints_text.strip() else ""
    return _fill(_text("swe_task"), repo=task.repo or "unknown", repo_root=repo_root,
                 problem_statement=task.problem_statement.strip(), hints=hints)
