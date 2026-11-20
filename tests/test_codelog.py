"""agent_code/: the code each step sent to the sandbox worker, one module per step, git-ignored."""

import subprocess

from agent_smith.codelog import CodeLog, code_root
from agent_smith.events import EventSink


def emit_run(sink):
    sink.emit("run_start", {"benchmark": "mbpp", "task_id": "11", "model": "m", "task_prompt": "Write f.", "tools": ["run_tests"]})
    sink.emit("llm_response", {"step": 1, "text": "Thought: try it.\n```python\nprint(1)\n```"})
    sink.emit("sandbox_start", {"step": 1, "code": "print(1)"})
    sink.emit("sandbox_end", {"step": 1, "status": "ok", "duration_s": 0.01, "output": "1\nsecond line"})
    sink.emit("llm_response", {"step": 2, "text": "Thought: done.\n```python\nfinal_answer('x')\n```"})
    sink.emit("sandbox_start", {"step": 2, "code": "final_answer('x')"})


def test_each_step_becomes_a_module(tmp_path):
    emit_run(EventSink(listeners=[CodeLog(tmp_path / "agent_code")]))
    [run] = list((tmp_path / "agent_code" / "mbpp").iterdir())
    assert run.name.endswith("_11")
    assert sorted(p.name for p in run.iterdir()) == ["step_01.py", "step_02.py", "task.md"]
    step1 = (run / "step_01.py").read_text()
    assert "# Thought:\n# try it." in step1 and "\nprint(1)\n" in step1
    assert "# ---- observation (ok" in step1 and "# second line" in step1
    compile(step1, "step_01.py", "exec")                         # a valid Python module
    assert "final_answer('x')" in (run / "step_02.py").read_text()  # written before it runs, too
    assert "Write f." in (run / "task.md").read_text()


def test_the_folder_ignores_itself(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    emit_run(EventSink(listeners=[CodeLog(tmp_path / "agent_code")]))
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert out == ""


def test_env_moves_or_disables_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SMITH_CODE_DIR", str(tmp_path / "x"))
    assert code_root() == tmp_path / "x"
    monkeypatch.setenv("AGENT_SMITH_CODE_DIR", "off")
    assert code_root() is None
    emit_run(EventSink(listeners=[CodeLog(code_root())]))       # disabled: nothing written, nothing raised
    monkeypatch.delenv("AGENT_SMITH_CODE_DIR")
    assert code_root(tmp_path).name == "agent_code" and code_root(tmp_path).parent == tmp_path


def test_folder_names_stay_readable(tmp_path):
    log = CodeLog(tmp_path, label="Запусти Agent Smith на MBPP задаче 2")
    log("run_start", {"benchmark": "studio", "task_id": "x"})
    assert log.dir.name.endswith("_Agent-Smith-MBPP-2")
