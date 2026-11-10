"""The mandatory SWE-bench tools (subject V.5), as plain Python functions.

They are wrapped by ``mcp_tools_swebench.py`` but have no dependency on MCP or
on the agent loop, so they can be tested independently.

Output formats follow the subject:

* ``read_file``      -> ``<line_number>: <line_content>``
* search tools       -> ``/absolute/path.py:<line_number> <line_content>``
* ``run_command``    -> stdout, stderr and exit code

Every truncation is announced explicitly in the output.
"""

from __future__ import annotations

import difflib
import os
import re
from typing import List, Optional

from agent_smith.tools.executor import Executor, quote

MAX_READ_LINES = 400
MAX_MATCHES = 150
MAX_LINE_CHARS = 300
MAX_OUTPUT_CHARS = 12_000
EXCLUDE_DIRS = (".git", "__pycache__", "node_modules", ".tox", ".eggs", "build", "dist")
START_TEST_OUTPUT = ">>>>> Start Test Output"
END_TEST_OUTPUT = ">>>>> End Test Output"


def truncate_middle(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = limit * 2 // 5, limit * 3 // 5
    return (text[:head] + f"\n\n[... output truncated: {len(text)} chars total, showing first {head} "
            f"and last {tail} ...]\n\n" + text[-tail:])


class SWEBenchTools:
    def __init__(self, executor: Executor, eval_script: Optional[str] = None):
        self.ex = executor
        self.eval_script = eval_script
        self.details: dict = {}  # provenance of the last call's inputs (reported in MCP _meta)

    # ------------------------------------------------------------------ #
    # File system
    # ------------------------------------------------------------------ #
    def read_file(self, filepath: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
        path = self.ex.resolve(filepath)
        if self.ex.is_dir(path):
            return f"Error: {path} is a directory. Use list_files('{filepath}') instead."
        try:
            lines = self.ex.read_text(path).splitlines()
        except (OSError, FileNotFoundError) as exc:
            return f"Error: cannot read {path}: {exc}"
        total = len(lines)
        start = max(1, int(start_line or 1))
        end = total if end_line in (None, -1, 0) else min(int(end_line), total)
        if total == 0:
            return f"(file {path} is empty)"
        if start > total:
            return f"Error: start_line {start} is beyond the end of the file ({total} lines)."
        note = ""
        if end - start + 1 > MAX_READ_LINES:
            end = start + MAX_READ_LINES - 1
            note = (f"\n[truncated: showing lines {start}-{end} of {total}. "
                    f"Call read_file('{filepath}', {end + 1}, {min(total, end + MAX_READ_LINES)}) for more]")
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return body + note

    def edit_file(self, filepath: str, old_str: str, new_str: str) -> str:
        path = self.ex.resolve(filepath)
        if not self.ex.exists(path):
            if old_str == "":
                self.ex.write_text(path, new_str)
                return f"Created new file {path} ({len(new_str.splitlines())} lines)."
            return f"Error: file {path} does not exist."
        try:
            original = self.ex.read_text(path)
        except OSError as exc:
            return f"Error: cannot read {path}: {exc}"
        if old_str == "":
            return "Error: old_str is empty. Provide the exact text to replace (include enough context)."
        count = original.count(old_str)
        if count == 0:
            return self._no_match(path, original, old_str)
        if count > 1:
            starts = [m.start() for m in re.finditer(re.escape(old_str), original)]
            lines = [original.count("\n", 0, s) + 1 for s in starts]
            return (f"Error: old_str occurs {count} times in {path} (lines {', '.join(map(str, lines))}). "
                    f"Include more surrounding lines so it matches exactly once.")
        updated = original.replace(old_str, new_str, 1)
        lint = self._syntax_problem(path, original, updated)
        if lint:
            return (f"Error: the edit was NOT applied because it would introduce a syntax error in {path}:\n"
                    f"{lint}\nFix the indentation/syntax of new_str and try again.")
        self.ex.write_text(path, updated)
        self._drop_bytecode(path)
        first = original[: original.index(old_str)].count("\n") + 1
        last = first + new_str.count("\n")
        snippet_start, snippet_end = max(1, first - 3), last + 3
        new_lines = updated.splitlines()
        snippet = "\n".join(f"{i}: {new_lines[i - 1]}" for i in range(snippet_start, min(snippet_end, len(new_lines)) + 1))
        return f"Edited {path} (replaced 1 occurrence, now lines {first}-{last}). Snippet:\n{snippet}"

    def _drop_bytecode(self, path: str) -> None:
        """Remove cached .pyc files: an edit that keeps the file size within the same second
        would otherwise be ignored by Python's mtime+size cache check."""
        if path.endswith(".py"):
            stem = os.path.splitext(os.path.basename(path))[0]
            cache = os.path.join(os.path.dirname(path), "__pycache__")
            self.ex.run(f"rm -f {quote(cache)}/{quote(stem)}.*.pyc", timeout=30)

    @staticmethod
    def _syntax_problem(path: str, before: str, after: str) -> Optional[str]:
        """Return a description if ``after`` fails to compile while ``before`` compiled."""
        if not path.endswith(".py"):
            return None
        try:
            compile(before, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            return None  # file was already not parseable by this interpreter: don't block edits
        try:
            compile(after, path, "exec", dont_inherit=True)
        except SyntaxError as exc:
            lines = after.splitlines()
            ctx = ""
            if exc.lineno:
                lo, hi = max(1, exc.lineno - 2), min(len(lines), exc.lineno + 2)
                ctx = "\n".join(f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1))
            return f"SyntaxError: {exc.msg} at line {exc.lineno}\n{ctx}"
        except ValueError as exc:
            return str(exc)
        return None

    @staticmethod
    def _no_match(path: str, original: str, old_str: str) -> str:
        msg = f"Error: old_str was not found in {path}."
        lines = original.splitlines()
        first = old_str.strip().splitlines()[0].strip() if old_str.strip() else ""
        if first:
            candidates = [(i + 1, l) for i, l in enumerate(lines) if first in l]
            if not candidates:
                close = difflib.get_close_matches(first, [l.strip() for l in lines], n=3, cutoff=0.6)
                candidates = [(i + 1, l) for i, l in enumerate(lines) if l.strip() in close][:3]
            if candidates:
                msg += " Similar lines (check whitespace/indentation):\n" + "\n".join(
                    f"{n}: {l}" for n, l in candidates[:5])
        return msg + "\nUse read_file to copy the exact text."

    def list_files(self, directory: str = ".", pattern: str = "*") -> str:
        root = self.ex.resolve(directory)
        if not self.ex.is_dir(root):
            return f"Error: {root} is not a directory."
        prune = " -o ".join(f"-name {quote(d)}" for d in EXCLUDE_DIRS)
        test = f"-path {quote(pattern if pattern.startswith('*') else '*/' + pattern)}" if "/" in pattern \
            else f"-name {quote(pattern or '*')}"
        cmd = f"find {quote(root)} \\( {prune} \\) -prune -o -type f {test} -print 2>/dev/null | sort | head -n {MAX_MATCHES * 4 + 1}"
        res = self.ex.run(cmd, timeout=60)
        files = [f for f in res.stdout.splitlines() if f]
        if not files:
            return f"No files matching '{pattern}' under {root}."
        note = ""
        if len(files) > MAX_MATCHES * 4:
            files = files[: MAX_MATCHES * 4]
            note = f"\n[truncated: more than {len(files)} files; use a narrower directory or pattern]"
        return "\n".join(files) + note

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #
    def _grep(self, args: str, root: str) -> List[str]:
        excl = " ".join(f"--exclude-dir={quote(d)}" for d in EXCLUDE_DIRS)
        res = self.ex.run(f"grep -rnI {excl} {args} {quote(root)} 2>&1 | head -n {MAX_MATCHES + 1}", timeout=90)
        return [l for l in res.stdout.splitlines() if l]

    @staticmethod
    def _format_grep(lines: List[str]) -> str:
        out = []
        for line in lines[:MAX_MATCHES]:
            m = re.match(r"^(.*?):(\d+):(.*)$", line)
            if not m:
                out.append(line)
                continue
            content = m.group(3)
            if len(content) > MAX_LINE_CHARS:
                content = content[:MAX_LINE_CHARS] + " [...]"
            out.append(f"{m.group(1)}:{m.group(2)} {content}")
        if len(lines) > MAX_MATCHES:
            out.append(f"[truncated: more than {MAX_MATCHES} matches; refine the pattern or file_pattern]")
        return "\n".join(out)

    def search_code(self, pattern: str, file_pattern: str = "*.py") -> str:
        include = f"--include={quote(file_pattern)}" if file_pattern and file_pattern != "*" else ""
        lines = self._grep(f"-E {include} -e {quote(pattern)}", self.ex.root)
        prefix = ""
        invalid = bool(lines) and lines[0].startswith("grep:")
        if invalid or not lines:
            # Agents often search for literal code such as "def f(x):", where ( ) are regex groups:
            # retry as plain text before answering "no matches".
            literal = self._grep(f"-F {include} -e {quote(pattern)}", self.ex.root)
            if literal and not literal[0].startswith("grep:"):
                lines = literal
                prefix = ("[note: the pattern is not a valid extended regex; searched it as literal text]\n" if invalid
                          else "[note: no regex match; these are literal-text matches]\n")
            elif invalid:
                lines = []
        if not lines:
            return f"No matches for '{pattern}' in files matching '{file_pattern}' (searched as regex and as literal text)."
        return prefix + self._format_grep(lines)

    def search_function_or_class_definition_in_code(self, name: str) -> str:
        short = name.strip().split(".")[-1]
        pattern = rf"^[[:space:]]*(async[[:space:]]+)?(def|class)[[:space:]]+{re.escape(short)}\b"
        lines = self._grep(f"-E --include='*.py' -e {quote(pattern)}", self.ex.root)
        if not lines:
            # Fall back to assignments like `name = ...` (aliases, lambdas).
            lines = self._grep(f"-E --include='*.py' -e {quote(rf'^[[:space:]]*{re.escape(short)}[[:space:]]*=')}",
                               self.ex.root)
            if not lines:
                return f"No definition found for '{name}'."
        return self._format_grep(lines)

    def find_references(self, name: str, filepath: Optional[str] = None, line: Optional[int] = None) -> str:
        short = name.strip().split(".")[-1]
        lines = self._grep(f"-wF --include='*.py' -e {quote(short)}", self.ex.root)
        if not lines:
            return f"No references to '{name}' found."
        if filepath:
            # Put matches from the given file (closest to `line`) first.
            target = self.ex.resolve(filepath)

            def key(l: str):
                m = re.match(r"^(.*?):(\d+):", l)
                if not m:
                    return (2, 0)
                same = m.group(1) == target
                dist = abs(int(m.group(2)) - int(line)) if (same and line) else 0
                return (0 if same else 1, dist)

            lines = sorted(lines[:MAX_MATCHES], key=key) + lines[MAX_MATCHES:]
        return self._format_grep(lines)

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def run_tests(self) -> str:
        script = self.eval_script
        self.details = {}
        if script:
            self.ex.write_text("/tmp/agent_eval.sh", script)
            cmd = "bash /tmp/agent_eval.sh"
            self.details = {"source": "eval_script field of the SWE-bench task.json (dumped by the moulinette, "
                                      "passed to the server via SWEBENCH_EVAL_SCRIPT_FILE); copied to /tmp/agent_eval.sh",
                            "script": script[:6000]}
        elif self.ex.exists("/eval.sh"):
            cmd = "bash /eval.sh"
            self.details = {"source": "/eval.sh found inside the container (SWE-bench harness location)"}
        else:
            return ("Error: no evaluation script configured (set SWEBENCH_EVAL_SCRIPT_FILE or provide /eval.sh). "
                    "Use run_command('python -m pytest <test file>') to run tests directly.")
        res = self.ex.run(cmd, timeout=float(os.environ.get("SWEBENCH_TEST_TIMEOUT", "600")))
        output = res.stdout + ("\n" + res.stderr if res.stderr.strip() else "")
        if START_TEST_OUTPUT in output:
            output = output.split(START_TEST_OUTPUT, 1)[1].split(END_TEST_OUTPUT, 1)[0]
        summary = summarize_test_output(output)
        header = f"exit_code: {res.exit_code}" + (" (TIMED OUT)" if res.timed_out else "")
        return f"{header}\n{summary}\n--- test output (tail) ---\n{truncate_middle(output.strip(), 8000)}"

    def get_patch(self) -> str:
        res = self.ex.run("git -c core.fileMode=false diff", workdir=self.ex.root, timeout=60)
        if res.exit_code != 0:
            return f"Error: git diff failed: {res.stderr.strip()}"
        return res.stdout

    def run_command(self, command: str, workdir: Optional[str] = None, timeout: int = 120) -> str:
        wd = self.ex.resolve(workdir) if workdir else self.ex.root
        res = self.ex.run(command, workdir=wd, timeout=min(int(timeout or 120), 600))
        return (f"exit_code: {res.exit_code}" + (" (TIMED OUT)" if res.timed_out else "") +
                f"\nstdout:\n{truncate_middle(res.stdout, 8000)}\nstderr:\n{truncate_middle(res.stderr, 4000)}")


_RESULT_RE = re.compile(r"^(.*?)\s+(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b|^(PASSED|FAILED|ERROR)\s+(\S+)")


def summarize_test_output(output: str) -> str:
    """Count pass/fail lines (pytest -rA style and sympy's bin/test style)."""
    failed, passed = [], 0
    for line in output.splitlines():
        s = line.strip()
        if s.startswith(("FAILED ", "ERROR ")):
            failed.append(s)
        elif s.startswith("PASSED ") or s.endswith(" PASSED") or s.endswith(" ok"):
            passed += 1
        elif re.match(r"^test_\w+ (F|E)$", s) or s.endswith(" FAILED") or s.endswith(" ERROR"):
            failed.append(s)
    tail = [l for l in output.splitlines() if re.search(r"(passed|failed|error)", l, re.I) and "==" in l][-1:]
    lines = [f"summary: {passed} passed, {len(failed)} failed/errored" + (f" | {tail[0].strip()}" if tail else "")]
    if failed:
        lines.append("failing tests:\n  " + "\n  ".join(failed[:30]))
    return "\n".join(lines)
