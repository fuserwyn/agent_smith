"""MBPP agent CLI.

    uv run python -m agent_mbpp --task-file ../cache/mbpp_task.json --output ../cache/mbpp_solution.json \\
        --model-name "model/name" --provider-url "https://provider.api/v1"
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from typing import Optional

from agent_smith.agent import AgentSettings, CodeAgent
from agent_smith.agent.prompts import mbpp_system_prompt, mbpp_task_prompt
from agent_smith.config import load_sandbox_config
from agent_smith.llm import build_client
from agent_smith.mcp_client import MCPClient
from agent_smith.models import MBPP_LIMITS, MBPPTaskInput, SolutionOutput
from agent_smith.runner import common_parser, default_server, run_cli, server_env, setup
from agent_smith.sandbox import Sandbox

MBPP_SETTINGS = AgentSettings(
    max_output_tokens_per_call=800,
    observation_max_chars=1500,
    old_observation_chars=400,
    keep_recent_steps=2,
    safety_seconds=10.0,
    idle_hint=("Nothing was tested or submitted. Put the full solution in a string `code`, call "
               "result = run_tests(code), print(result), and if it passed call final_answer(code) — in ONE block."),
)


def best_candidate(sandbox: Sandbox) -> Optional[str]:
    """Fallback when final_answer was never called: last code that passed run_tests, else the last tried."""
    tried = [c for c in sandbox.tool_log if c.name == "run_tests" and isinstance(c.args.get("code"), str)]
    passed = [c for c in tried if c.result.startswith("ALL TESTS PASSED")]
    pick = (passed or tried or [None])[-1]
    return pick.args["code"] if pick else None


def main(argv=None) -> int:
    started = time.monotonic()
    args = common_parser("agent_mbpp", "Solve an MBPP task with the Agent Smith code agent").parse_args(argv)
    events = setup(args)
    with open(args.task_file, encoding="utf-8") as fh:
        raw = json.load(fh)
    task_id = str(raw.get("task_id", "unknown"))

    def body() -> SolutionOutput:
        task = MBPPTaskInput.model_validate(raw)
        config = load_sandbox_config(args.sandbox_config)
        limits = MBPP_LIMITS.model_copy()
        if args.max_iterations:
            limits.max_iterations = min(args.max_iterations, MBPP_LIMITS.max_iterations)
        llm = build_client(args.model_name, args.provider_url, use_fallbacks=args.fallback, on_event=events.emit)

        with tempfile.TemporaryDirectory(prefix="agent-mbpp-") as tmp:
            task_path = os.path.join(tmp, "task.json")
            with open(task_path, "w", encoding="utf-8") as fh:
                fh.write(task.model_dump_json())
            env = server_env({"MBPP_TASK_FILE": task_path})
            if args.mcp_server:
                mcp = MCPClient(url=args.mcp_server)
            else:
                mcp = MCPClient(stdio_command=args.mcp_stdio or default_server("mcp_tools_mbpp.py"), env=env)
            with mcp.connect():
                events.emit("mcp_connected", mcp.describe())
                with Sandbox(config, mcp, event_hook=events.emit, trace_execution=bool(args.trace_file)) as sandbox:
                    agent = CodeAgent(llm, sandbox, mbpp_system_prompt(config, mcp), limits, "mbpp", task_id,
                                      settings=MBPP_SETTINGS, events=events,
                                      fallback_answer=lambda: best_candidate(sandbox), started=started)
                    output = agent.run(mbpp_task_prompt(task))
                    if agent.interrupted:
                        raise KeyboardInterrupt
                    return output

    return run_cli("mbpp", task_id, args, started, body, events)


if __name__ == "__main__":
    sys.exit(main())
