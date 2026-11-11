"""SWE-bench agent CLI.

    uv run python -m agent_swebench --task-file ../cache/swebench_task.json \\
        --output ../cache/swebench_solution.json --model-name "model/name" --provider-url "https://provider.api/v1"

Architecture choice (subject V.4, option b): the sandbox runs on the host and
the MCP tools bridge into the task's Docker container via ``docker exec``.
The container is removed when the agent finishes (also on errors / SIGTERM).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from typing import Optional

from agent_smith.agent import AgentSettings, CodeAgent
from agent_smith.agent.prompts import swebench_system_prompt, swebench_task_prompt
from agent_smith.config import load_sandbox_config
from agent_smith.docker_env import DockerEnv
from agent_smith.llm import build_client
from agent_smith.mcp_client import MCPClient
from agent_smith.models import SWEBENCH_LIMITS, SandboxConfig, SolutionOutput, SWEBenchTaskInput
from agent_smith.runner import common_parser, default_server, run_cli, server_env, setup
from agent_smith.sandbox import Sandbox

REPO_ROOT = "/testbed"
SWE_SETTINGS = AgentSettings(
    max_output_tokens_per_call=1500,
    observation_max_chars=7000,
    old_observation_chars=800,
    keep_recent_steps=3,
    show_budget=True,
    safety_seconds=30.0,
)


def main(argv=None) -> int:
    parser = common_parser("agent_swebench", "Fix a SWE-bench issue with the Agent Smith code agent")
    parser.add_argument("--keep-network", action="store_true", help="do not start the container with --network none")
    parser.add_argument("--prompt-variant", choices=["full", "minimal"], default="full",
                        help="system prompt variant (minimal = no method section; ablation study)")
    args = parser.parse_args(argv)
    events = setup(args)
    with open(args.task_file, encoding="utf-8") as fh:
        raw = json.load(fh)
    task_id = str(raw.get("instance_id", "unknown"))
    started = time.monotonic()

    def body() -> SolutionOutput:
        nonlocal started
        task = SWEBenchTaskInput.model_validate(raw)
        config: SandboxConfig = load_sandbox_config(args.sandbox_config)
        limits = SWEBENCH_LIMITS.model_copy()
        if args.max_iterations:
            limits.max_iterations = min(args.max_iterations, SWEBENCH_LIMITS.max_iterations)
        llm = build_client(args.model_name, args.provider_url, use_fallbacks=args.fallback, on_event=events.emit)

        docker = DockerEnv(task.docker_image, task.instance_id, network="bridge" if args.keep_network else "none")
        events.emit("docker_pull", {"image": task.docker_image})
        docker.ensure_image()
        # The clock starts once the image is available locally (pulling is infrastructure, not agent work).
        started = time.monotonic()
        try:
            container = docker.start()
            events.emit("docker_started", {"container": container, "image": task.docker_image})
            with tempfile.TemporaryDirectory(prefix="agent-swe-") as tmp:
                eval_path = os.path.join(tmp, "eval.sh")
                with open(eval_path, "w", encoding="utf-8") as fh:
                    fh.write(task.eval_script)
                env = server_env({"SWEBENCH_CONTAINER": container, "TESTBED_PATH": REPO_ROOT,
                                  "SWEBENCH_EVAL_SCRIPT_FILE": eval_path})
                if args.mcp_server:
                    mcp = MCPClient(url=args.mcp_server)
                else:
                    mcp = MCPClient(stdio_command=args.mcp_stdio or default_server("mcp_tools_swebench.py"), env=env)
                with mcp.connect():
                    events.emit("mcp_connected", mcp.describe())

                    def current_patch() -> str:
                        if "get_patch" not in mcp.tool_names():
                            return ""
                        out = mcp.call_tool("get_patch", {})
                        return "" if out.is_error or "diff --git" not in out.text else out.text

                    def patch_only(answer: str) -> Optional[str]:
                        if "diff --git" in answer:
                            return None
                        return ("the final answer must be the git patch, not a description. "
                                "Call final_answer(get_patch()) (check first that get_patch() is not empty).")

                    with Sandbox(config, mcp, event_hook=events.emit, trace_execution=bool(args.trace_file)) as sandbox:
                        agent = CodeAgent(llm, sandbox, swebench_system_prompt(config, mcp, REPO_ROOT, args.prompt_variant), limits,
                                          "swebench", task.instance_id, settings=SWE_SETTINGS, events=events,
                                          fallback_answer=current_patch, started=started,
                                          answer_validator=patch_only)
                        output = agent.run(swebench_task_prompt(task, REPO_ROOT))
                        if agent.interrupted:
                            raise KeyboardInterrupt
                        return output
        finally:
            docker.close()
            events.emit("docker_removed", {"container": docker.name})

    return run_cli("swebench", task_id, args, started, body, events)


if __name__ == "__main__":
    sys.exit(main())
