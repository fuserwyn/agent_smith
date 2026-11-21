"""Studio chat agent end to end with a scripted LLM: real loop, sandbox, MCP tools, checkpoints and undo."""

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_smith.studio.agent import AgentService  # noqa: E402
from agent_smith.studio.app import create_app  # noqa: E402
from tests.fake_llm_server import FakeLLMServer  # noqa: E402

TOKEN = "agent-token"
H = {"Authorization": f"Bearer {TOKEN}", "X-Studio": "1"}


def step(code: str) -> str:
    return f"Thought: next.\n```python\n{code}\n```"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "notes.md").write_text("keep me\n")
    return root


def make_client(project, tmp_path, monkeypatch, script):
    srv = FakeLLMServer(script)
    srv.__enter__()
    monkeypatch.setattr(AgentService, "models", staticmethod(lambda: {
        "models": [{"provider": "fake", "model": "fake-model", "url": srv.url, "key_ok": True, "paid": False, "key_env": None, "notes": ""}],
        "default": {"provider": "fake", "model": "fake-model"}}))
    app = create_app(project, TOKEN, web_dist=None, shell="local", state_dir=tmp_path / "state")
    return TestClient(app, base_url="http://127.0.0.1:8790"), app, srv


def wait_done(client, run_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get("/api/agent/state", headers=H).json()["runs"][run_id]
        if run["status"] != "running":
            return run
        time.sleep(0.1)
    pytest.fail("agent run did not finish")


def test_agent_mode_edits_and_the_turn_can_be_reviewed_and_undone(project, tmp_path, monkeypatch):
    client, _app, srv = make_client(project, tmp_path, monkeypatch, [
        step("print(read_file('calc.py'))"),
        step("print(edit_file('calc.py', 'return a + b', 'return a - b'))\nprint(edit_file('new_helper.py', '', 'X = 1\\n'))"),
        step("final_answer('I changed **calc.py** (add now subtracts) and created new_helper.py.')"),
    ])
    with client, srv:
        run = client.post("/api/agent/runs", json={"message": "make add subtract", "mode": "agent"}, headers=H).json()
        done = wait_done(client, run["id"])
        assert done["status"] == "done", done
        assert "calc.py" in done["answer"] and done["steps"] == 3 and done["checkpoint"]
        assert (project / "calc.py").read_text() == "def add(a, b):\n    return a - b\n"
        assert {c["path"]: c["status"] for c in done["changes"]} == {"calc.py": "modified", "new_helper.py": "added"}
        d = client.get(f"/api/agent/runs/{run['id']}/diff", params={"path": "calc.py"}, headers=H).json()
        assert "a + b" in d["original"] and "a - b" in d["modified"]
        # undo one file, then the rest of the turn
        client.post(f"/api/agent/runs/{run['id']}/undo", json={"paths": ["new_helper.py"]}, headers=H)
        assert not (project / "new_helper.py").exists() and "a - b" in (project / "calc.py").read_text()
        r = client.post(f"/api/agent/runs/{run['id']}/undo", json={}, headers=H).json()
        assert r["changes"] == [] and (project / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
        assert (project / "notes.md").read_text() == "keep me\n"
        msgs = client.get("/api/agent/state", headers=H).json()["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant"] and "calc.py" in msgs[1]["text"]
        assert "read_file" in client.get(f"/api/agent/runs/{run['id']}/trace", headers=H).text
        assert '"type": "workspace", "executor": "local"' in client.get(f"/api/agent/runs/{run['id']}/trace", headers=H).text
    assert not (project / ".git").exists()  # checkpoints live outside the project


def test_ask_mode_cannot_change_anything(project, tmp_path, monkeypatch):
    client, _app, srv = make_client(project, tmp_path, monkeypatch, [
        step("print(edit_file('calc.py', 'a + b', 'HACKED'))"),
        step("print(run_command('rm calc.py'))"),
        step("final_answer('add() returns the sum of its arguments.')"),
    ])
    with client, srv:
        run = client.post("/api/agent/runs", json={"message": "what does add do?", "mode": "ask"}, headers=H).json()
        done = wait_done(client, run["id"])
        assert done["status"] == "done" and done["checkpoint"] is None
        assert (project / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
        system = srv.requests[0]["messages"][0]["content"]
        assert "read_file" in system and "edit_file" not in system and "run_command" not in system


def test_stop_prevents_running_the_model_code(project, tmp_path, monkeypatch):
    client, app, srv = make_client(project, tmp_path, monkeypatch, [step("print(edit_file('calc.py', 'a + b', 'STOPPED'))")] * 5)
    with client, srv:
        original = AgentService._work

        def slow_start(self, run, url, history):  # stop arrives before the first step
            self.stop(run.id)
            return original(self, run, url, history)

        monkeypatch.setattr(AgentService, "_work", slow_start)
        run = client.post("/api/agent/runs", json={"message": "change it", "mode": "agent"}, headers=H).json()
        done = wait_done(client, run["id"])
        assert done["status"] == "stopped" and "STOPPED" not in (project / "calc.py").read_text()


def test_one_run_at_a_time_and_validation(project, tmp_path, monkeypatch):
    client, app, srv = make_client(project, tmp_path, monkeypatch, [step("final_answer('ok')")])
    with client, srv:
        assert client.post("/api/agent/runs", json={"message": "  ", "mode": "agent"}, headers=H).status_code == 400
        assert client.post("/api/agent/runs", json={"message": "hi", "mode": "yolo"}, headers=H).status_code == 422
        assert client.post("/api/agent/runs", json={"message": "hi", "mode": "ask", "provider": "x", "model": "y"}, headers=H).status_code == 400
        app.state.agent.current = "busy"
        assert client.post("/api/agent/runs", json={"message": "hi", "mode": "ask"}, headers=H).status_code == 409
        app.state.agent.current = None
        assert client.post("/api/agent/runs", json={"message": "hi"}).status_code == 401


def test_default_model_falls_back_to_the_custom_model_list(tmp_path, monkeypatch):
    f = tmp_path / "models.json"
    f.write_text('{"providers": [{"name": "local", "base_url": "http://127.0.0.1:9/v1", "key_env": null, "hosts": ["127.0.0.1"]}],'
                 ' "models": [{"provider": "local", "model": "my-model"}], "fallbacks": [], "fallback_enabled": false}')
    monkeypatch.setenv("AGENT_SMITH_MODELS_FILE", str(f))
    assert AgentService.models()["default"] == {"provider": "local", "model": "my-model"}


def test_a_finished_run_already_lists_its_changes(project, tmp_path, monkeypatch):
    """Regression: the status used to turn 'done' before the changed files were computed."""
    from agent_smith.studio.checkpoints import Checkpoints
    slow = Checkpoints.changes

    def slow_changes(self, since, only=None):
        time.sleep(0.5)
        return slow(self, since, only)

    monkeypatch.setattr(Checkpoints, "changes", slow_changes)
    client, _app, srv = make_client(project, tmp_path, monkeypatch, [
        step("print(edit_file('calc.py', 'return a + b', 'return a - b'))"),
        step("final_answer('done')"),
    ])
    with client, srv:
        run = client.post("/api/agent/runs", json={"message": "subtract", "mode": "agent"}, headers=H).json()
        done = wait_done(client, run["id"])
        assert done["status"] == "done" and [c["path"] for c in done["changes"]] == ["calc.py"]


def test_a_reply_cut_by_the_output_limit_is_reported_and_files_can_be_written_in_parts(project, tmp_path, monkeypatch):
    cut = {"text": "Thought: big file.\n```python\nwrite_file('page.html', '''<html>\n" + "x" * 50, "finish_reason": "length"}
    client, _app, srv = make_client(project, tmp_path, monkeypatch, [
        cut,
        step("print(write_file('/workspace/site/page.html', '<html>\\n'))"),
        step("print(append_file('site/page.html', '</html>\\n'))\nprint(write_file('../escape.txt', 'no'))"),
        step("final_answer('done')"),
    ])
    with client, srv:
        run = client.post("/api/agent/runs", json={"message": "write a page", "mode": "agent"}, headers=H).json()
        assert wait_done(client, run["id"])["status"] == "done"
        assert "cut off at the output limit" in srv.requests[1]["messages"][-1]["content"]
        assert (project / "site" / "page.html").read_text() == "<html>\n</html>\n"
        obs = srv.requests[3]["messages"][-1]["content"]
        assert "appended 8 chars to site/page.html" in obs and "write_file failed" in obs
        assert not (tmp_path / "escape.txt").exists()
        assert "append_file(" in srv.requests[0]["messages"][0]["content"]


def test_only_what_the_agent_changed_is_its_turn_not_edits_made_meanwhile(project, tmp_path, monkeypatch):
    """Regression: files edited by someone else during a turn (you, another program) were listed as the
    agent's changes, and "Undo all" would have reverted them."""
    import threading

    client, _app, srv = make_client(project, tmp_path, monkeypatch, [
        step("print(edit_file('calc.py', 'return a + b', 'return a - b'))"),
        step("final_answer('done')"),
    ])
    srv.delay = 0.6  # the model "thinks": someone edits another file meanwhile
    notes = project / "NOTES.md"
    notes.write_text("mine\n")
    with client, srv:
        run = client.post("/api/agent/runs", json={"message": "subtract", "mode": "agent"}, headers=H).json()
        threading.Timer(0.3, lambda: notes.write_text("mine, edited during the turn\n")).start()
        done = wait_done(client, run["id"])
        assert [c["path"] for c in done["changes"]] == ["calc.py"]          # not NOTES.md
        client.post(f"/api/agent/runs/{run['id']}/undo", json={"paths": None}, headers=H)
        assert notes.read_text() == "mine, edited during the turn\n"     # Undo all left it alone
        assert "a + b" in (project / "calc.py").read_text()                 # and reverted the agent's edit
