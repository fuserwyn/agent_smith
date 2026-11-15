"""UI server: launch recipes, request guards, recorded traces, job runner."""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agent_smith import ui_jobs, ui_server


@pytest.fixture()
def server(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.make_handler(tmp_path / "live.jsonl"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def call(base, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **({"X-Agent-Smith": "1"} if body is not None else {}), **(headers or {})}
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=h,
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_preview_builds_fixed_commands():
    pv = ui_jobs.preview("mbpp", {"task_id": "80"})
    cmds = [s["command"] for s in pv["steps"]]
    assert cmds[0] == ("(cd moulinette && PYTHONPATH=. uv run moulinette_eval dump mbpp --task-id 80 "
                       "--output ../runs/ui/manual-mbpp/task.json)")
    assert "--model-name qwen/qwen3.8-27b --provider-url https://api.groq.com/openai/v1" in cmds[1]
    assert cmds[2].startswith("(cd moulinette && PYTHONPATH=. DOCKER_HOST=")  # the moulinette validates in Docker
    assert "uv run moulinette_eval validate mbpp ../runs/ui/manual-mbpp/task.json" in cmds[2]
    assert [s["key"] for s in ui_jobs.preview("sandbox", {})["steps"]] == ["step_sandbox"]


@pytest.mark.parametrize("action,params", [
    ("mbpp", {"task_id": "1; rm -rf /"}),
    ("mbpp", {"provider": "groq", "model": "not-in-config"}),
    ("swebench", {"task_id": "$(id)"}),
    ("bash", {}),
])
def test_invalid_actions_are_rejected(action, params):
    with pytest.raises(ui_jobs.ActionError):
        ui_jobs.preview(action, params)


def test_trace_paths_are_confined():
    for bad in ("../.env", "benchmarks/../.env", "benchmarks/x/task.json", "/etc/passwd"):
        with pytest.raises(ui_jobs.ActionError):
            ui_jobs.safe_trace_path(bad)


def test_post_needs_header_and_local_origin(server):
    assert call(server, "/api/preview", {"action": "sandbox"})[0] == 200
    assert call(server, "/api/preview", {"action": "sandbox"}, {"X-Agent-Smith": "0"})[0] == 403
    assert call(server, "/api/preview", {"action": "sandbox"}, {"Origin": "https://evil.example"})[0] == 403
    assert call(server, "/api/info", None, {"Host": "evil.example"})[0] == 403  # DNS rebinding
    code, body = call(server, "/api/preview", {"action": "mbpp", "params": {"task_id": "x"}})
    assert code == 400 and "number" in body


def test_actions_and_runs_endpoints(server):
    code, body = call(server, "/api/actions")
    data = json.loads(body)
    assert code == 200 and data["defaults"]["mbpp"]["model"] and all("key_ok" in m for m in data["models"])
    assert "OPENROUTER_API_KEY=" not in body  # key presence only, never values
    code, body = call(server, "/api/trace?path=../.env")
    assert code == 400


def test_job_runs_steps_and_reads_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(ui_jobs, "JOBS_DIR", ui_jobs.ROOT / "runs" / "ui-test")
    monkeypatch.setattr(ui_jobs, "plan", lambda action, params, job_dir: [
        ui_jobs.Step("step_dump", [sys.executable, "-c", "print('dumped')"]),
        ui_jobs.Step("step_validate", [sys.executable, "-c", "print('Overall: PASSED'); raise SystemExit(1)"]),
    ])
    followed = []
    jobs = ui_jobs.JobManager(on_trace=followed.append)
    try:
        jobs.start("mbpp", {})
        for _ in range(100):
            if jobs.snapshot()["status"] != "running":
                break
            time.sleep(0.05)
        snap = jobs.snapshot()
        assert snap["status"] == "done" and snap["verdict"] == "PASSED"
        assert [s["status"] for s in snap["steps"]] == ["ok", "failed"]  # failed validation = verdict, not a crash
        assert "dumped" in snap["log"] and followed and followed[0].name == "events.jsonl"
        folder = ui_jobs.ROOT / snap["dir"]
        assert "Overall: PASSED" in (folder / "validation.txt").read_text() and "[exit 1]" in (folder / "output.log").read_text()
    finally:
        import shutil
        shutil.rmtree(ui_jobs.ROOT / "runs" / "ui-test", ignore_errors=True)
