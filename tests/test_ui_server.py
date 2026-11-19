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
    assert len(data["mbpp"]) > 400 and set(data["mbpp"][0]) == {"id", "split", "text"}  # statements only
    assert '"test_list"' not in body and '"code"' not in body  # never the hidden tests or the reference solution
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


def test_embedded_file_mode_commands_match_the_server_recipes():
    """ui/index.html carries the default commands for file:// mode; they must equal ui_jobs' recipes."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("embed", ui_jobs.ROOT / "scripts" / "embed_ui_commands.py")
    embed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(embed)
    stored = embed.embedded(embed.UI.read_text(encoding="utf-8"))
    assert stored == ui_jobs.static_commands(), "run: uv run python scripts/embed_ui_commands.py"
    assert "docker context inspect" in stored["swebench"]["steps"][-1]["command"]


def test_paid_model_is_flagged_and_free_fallback_is_on():
    from agent_smith.config import load_models_config
    models = ui_jobs.model_choices()
    paid = [m for m in models if m["paid"]]
    assert paid and all(m["provider"] == "openrouter" for m in paid)
    assert ui_jobs.DEFAULTS["mbpp"][1] not in {m["model"] for m in paid}  # defaults stay free
    assert ui_jobs.DEFAULTS["swebench"][1] not in {m["model"] for m in paid}
    cmd = ui_jobs.preview("mbpp", {"provider": "openrouter", "model": paid[0]["model"]})["steps"][1]["command"]
    assert f"--model-name {paid[0]['model']} --provider-url https://openrouter.ai/api/v1" in cmd
    cfg = load_models_config()
    assert cfg.fallback_enabled and not any(f.paid for f in cfg.fallbacks)  # running out of quota never lands on paid


def test_mock_exam_free_by_default_paid_on_request():
    free = ui_jobs.preview("exam", {})["steps"][0]["command"]
    assert free.startswith("./scripts/mock_exam.sh all") and "--paid" not in free
    assert ui_jobs.preview("exam", {"tier": "paid"})["steps"][0]["command"].endswith(" --paid")
    with pytest.raises(ui_jobs.ActionError):
        ui_jobs.preview("exam", {"tier": "gold"})
