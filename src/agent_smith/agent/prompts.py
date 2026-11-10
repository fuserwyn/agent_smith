"""System prompts. The tool documentation part is generated from the MCP server (see sandbox.manual)."""

from __future__ import annotations

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
        method = '''Your FIRST reply must already test and submit. Always use exactly this pattern:
Thought: Count with a dict and return the most common item.
```python
code = r"""
from collections import Counter
def most_common(xs):
    return Counter(xs).most_common(1)[0][0]
"""
result = run_tests(code)
print(result)
if result.startswith("ALL TESTS PASSED"):
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


SWE_METHOD = """## Method (follow it; it is how an experienced engineer fixes a bug)
1. Understand: restate the bug in one sentence. Identify symbols, error messages and files mentioned in the issue.
2. Locate: search_function_or_class_definition_in_code / search_code for those symbols. Read only the relevant
   ranges with read_file(path, start, end) (not whole big files).
3. Reproduce: write a tiny script that shows the bug, e.g.
   run_command("cat > /tmp/repro.py << 'EOF'\\n<python>\\nEOF\\npython /tmp/repro.py"). Confirm you see the failure.
4. Fix: make the minimal, general change at the root cause with edit_file(path, old_str, new_str).
   old_str must be copied EXACTLY (indentation included) from read_file output and be unique.
5. Verify: re-run the reproduction, then run_tests(). Inspect failing test names; iterate if needed.
6. Submit: as soon as the fix is verified call final_answer(get_patch()). Do not keep exploring after tests pass.

## Efficiency
- Batch several tool calls in ONE code block (search + read, edit + re-run). Print only what you need.
- You have a limited number of steps and tokens: no long essays in Thoughts.
- Never modify test files. Keep scratch files in /tmp, never inside the repository.

## Integrity (mandatory)
- Solve the issue by reading and running the code in this repository only.
- Do not fetch anything from the internet, pull requests, issue trackers or package indexes, and do not
  reproduce a memorised upstream patch without verifying it against this code base."""

SWE_EXAMPLE = '''## Example of a good first step
Thought: The issue says `Matrix.col_insert` misplaces columns. Find its definition and read it.
```python
print(search_function_or_class_definition_in_code("col_insert"))
print(search_code("def _eval_col_insert", "*.py"))
```<end_code>'''


def swebench_system_prompt(config: SandboxConfig, mcp, repo_root: str = "/testbed") -> str:
    return f"""You are an autonomous software engineer fixing a real issue in the repository at {repo_root}.
You act by writing Python code that calls tools (Python functions). The repository lives in a separate
environment: you can only reach it through the tools, never with open() or imports.

{FORMAT_RULES}

{build_manual(config, mcp)}

{SWE_METHOD}

{SWE_EXAMPLE}"""


def swebench_task_prompt(task: SWEBenchTaskInput, repo_root: str = "/testbed") -> str:
    hints = f"\n\n<hints>\n{task.hints_text.strip()}\n</hints>" if task.hints_text.strip() else ""
    return (f"Repository: {task.repo or 'unknown'} (checked out at {repo_root})\n"
            f"Fix the following issue so that the repository's tests pass.\n\n"
            f"<issue>\n{task.problem_statement.strip()}\n</issue>{hints}\n\n"
            f"Start with step 1 of the method.")
