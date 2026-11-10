"""The mandatory tools tested independently (local executor on a temporary git repo)."""

import re

from agent_smith.tools.executor import LocalExecutor
from agent_smith.tools.swebench_tools import SWEBenchTools


def tools(repo, script=None):
    return SWEBenchTools(LocalExecutor(str(repo)), eval_script=script)


def test_read_file_format_and_ranges(git_repo):
    t = tools(git_repo)
    out = t.read_file("pkg/mathutil.py", 1, 2)
    assert out == "1: def add(a, b):\n2:     return a - b"
    assert t.read_file(str(git_repo / "pkg/mathutil.py"), 9).startswith("9: ")
    assert "Error" in t.read_file("nope.py")


def test_search_code_format(git_repo):
    out = tools(git_repo).search_code("return a - b", "*.py")
    assert re.fullmatch(r"/.*/pkg/mathutil\.py:2     return a - b", out)


def test_definition_and_references(git_repo):
    t = tools(git_repo)
    assert t.search_function_or_class_definition_in_code("Calculator").endswith("mathutil.py:5 class Calculator:")
    refs = t.find_references("add", "pkg/mathutil.py", 1)
    assert refs.count("\n") >= 2 and refs.splitlines()[0].split(":")[0].endswith("mathutil.py")


def test_list_files(git_repo):
    out = tools(git_repo).list_files(".", "*.py")
    assert "mathutil.py" in out and ".git" not in out


def test_edit_file_unique_missing_ambiguous_and_syntax(git_repo):
    t = tools(git_repo)
    assert "not found" in t.edit_file("pkg/mathutil.py", "return a * b", "x")
    assert "occurs" in t.edit_file("pkg/mathutil.py", "    ", "  ")
    bad = t.edit_file("pkg/mathutil.py", "    return a - b", "    return (a + b")
    assert "NOT applied" in bad and "SyntaxError" in bad
    ok = t.edit_file("pkg/mathutil.py", "    return a - b", "    return a + b")
    assert ok.startswith("Edited") and "return a + b" in (git_repo / "pkg/mathutil.py").read_text()


def test_patch_run_command_and_run_tests(git_repo):
    t = tools(git_repo, script="cd {} && python -m pytest -q tests 2>&1; echo done".format(git_repo))
    assert "failed" in t.run_tests().lower()
    t.edit_file("pkg/mathutil.py", "return a - b", "return a + b")
    patch = t.get_patch()
    assert patch.startswith("diff --git") and "+    return a + b" in patch
    out = t.run_command("echo hi; echo err >&2; exit 3")
    assert "exit_code: 3" in out and "hi" in out and "err" in out
    assert "1 passed" in t.run_tests()


def test_search_code_falls_back_to_literal_text(git_repo):
    out = tools(git_repo).search_code("def add(a, b):", "*.py")  # ( ) are regex groups: no regex match
    assert "literal-text matches" in out and "mathutil.py:1 def add(a, b):" in out
