"""Build the BENCHMARK_REPORT.md tables from real solution.json files.

Layout expected (written by scripts/run_benchmark.sh):

    benchmarks/<run-name>/<model-slug>/<instance_id>/solution.json
    benchmarks/<run-name>/<model-slug>/<instance_id>/validation.txt   (moulinette output, optional)

    uv run python scripts/benchmark_table.py benchmarks/main

Numbers come only from the files: nothing is estimated or filled in by hand.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional


def load(run_dir: Path) -> List[dict]:
    rows = []
    for sol_path in sorted(run_dir.glob("*/*/solution.json")):
        sol = json.loads(sol_path.read_text())
        val = sol_path.with_name("validation.txt")
        passed = None
        if val.exists():
            text = val.read_text()
            passed = bool(re.search(r"Overall:\s*\S*PASSED", text)) or "Overall: PASSED" in text
        rows.append({"model": sol_path.parent.parent.name, "task": sol_path.parent.name, "sol": sol, "passed": passed})
    return rows


def patch_files(patch: str) -> List[str]:
    return re.findall(r"^diff --git a/(\S+)", patch or "", re.M)


def first_touch_step(sol: dict) -> Optional[int]:
    """Step at which the agent first reads/edits a file that appears in the final patch."""
    files = patch_files(sol.get("solution", ""))
    for step in sol.get("steps", []):
        if any(f in step.get("sandbox_input", "") for f in files):
            return step["step"]
    return None


def tests_first_pass_step(sol: dict) -> Optional[int]:
    for step in sol.get("steps", []):
        out = step.get("sandbox_output", "")
        if re.search(r"summary: [1-9]\d* passed, 0 failed", out) or "ALL TESTS PASSED" in out:
            return step["step"]
    return None


def failures_first_decrease(sol: dict) -> Optional[int]:
    baseline = None
    for step in sol.get("steps", []):
        m = re.search(r"summary: \d+ passed, (\d+) failed", step.get("sandbox_output", ""))
        if not m:
            continue
        n = int(m.group(1))
        if baseline is None:
            baseline = n
        elif n < baseline:
            return step["step"]
    return None


def fmt(v) -> str:
    return "–" if v is None else str(v)


def main(run_dir: str) -> None:
    rows = load(Path(run_dir))
    if not rows:
        print(f"no solution.json under {run_dir}")
        return
    print("### Results\n")
    print("| Model | Task | Pass/Fail | Iterations | Input tokens | Output tokens | Time (s) |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        s = r["sol"]
        verdict = {True: "PASS", False: "FAIL", None: "not validated"}[r["passed"]]
        print(f"| {r['model']} | {r['task']} | {verdict} | {s['iterations']} | {s['total_input_tokens']:,} | "
              f"{s['total_output_tokens']:,} | {s['total_time_seconds']:.1f} |")

    print("\n### Provider reliability\n")
    print("| Model | Requests | Avg response time (ms) | Retries | Runs ending in LLM failure |")
    print("|---|---|---|---|---|")
    by_model: Dict[str, List[dict]] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r["sol"])
    for model, sols in by_model.items():
        times = [st["request_time_ms"] for s in sols for st in s["steps"]]
        retries = sum(st["retries"] for s in sols for st in s["steps"])
        failures = sum(1 for s in sols if (s.get("error") or "").startswith("LLM failure"))
        print(f"| {model} | {sum(s['total_requests'] for s in sols)} | {mean(times):.0f} | {retries} | "
              f"{failures}/{len(sols)} |" if times else f"| {model} | 0 | – | {retries} | {failures}/{len(sols)} |")

    print("\n### Intermediary metrics\n")
    print("| Model | Task | First touch of a patched file (step) | Failures first decrease (step) | "
          "Tests first pass (step) | Steps from first pass to final_answer |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        s = r["sol"]
        tp = tests_first_pass_step(s)
        gap = (s["iterations"] - tp) if (tp is not None and s["success"]) else None
        print(f"| {r['model']} | {r['task']} | {fmt(first_touch_step(s))} | {fmt(failures_first_decrease(s))} | "
              f"{fmt(tp)} | {fmt(gap)} |")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "benchmarks/swebench/main")
