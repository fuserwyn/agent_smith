"""Tables, statistics and charts of BENCHMARK_REPORT.md, computed from the run folders only.

    uv run python scripts/benchmark_report.py            # prints the Markdown, writes benchmarks/swebench/charts/*.svg

Input (written by scripts/run_benchmark.sh / run_benchmark_suite.sh):

    benchmarks/swebench/<run>/<model-slug>/<instance_id>/solution.json   the agent's output (every step)
    benchmarks/swebench/<run>/<model-slug>/<instance_id>/validation.txt  the moulinette's verdict ("Overall: PASSED")

Runs: v2 (main grid), v2-repeat-N (same reference model again), v2-ablation-<name> (one mechanism off).
Pass/fail is ALWAYS the moulinette's verdict; the agent's own `success` flag is reported separately.
Nothing is estimated or typed by hand: every number below is recomputed from those files.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmarks" / "swebench"
CHARTS = BENCH / "charts"
PRICES = json.loads((BENCH / "paid-equivalent-prices.json").read_text())
REFERENCE = "nvidia_nemotron-3.5-lightning-30b-a3b"
LIMITS = {"iterations": 30, "input": 300_000, "output": 10_000, "time": 900}
# free-tier quotas as documented by the providers (requests), used for "exams per day" in the cost section
QUOTA = {"integrate.api.nvidia.com": ("~40 requests/min, no daily cap observed", None),
         "openrouter.ai": (":free models: 1,000 requests/day (account with >= $10 credits; 50/day without)", 1000),
         "api.cohere.ai": ("trial key: 1,000 calls/month, 20/min", 1000 / 30)}


# --------------------------------------------------------------------------- loading
def load(run: str) -> List[dict]:
    rows = []
    for sol_path in sorted((BENCH / run).glob("*/*/solution.json")):
        sol = json.loads(sol_path.read_text())
        val = sol_path.with_name("validation.txt")
        text = re.sub(r"\x1b\[[0-9;]*m", "", val.read_text()) if val.exists() else ""
        verdict = re.search(r"^Overall:\s*(\w+)", text, re.M)
        steps = sol.get("steps") or []
        url = next((s.get("api_url") for s in steps if s.get("api_url")), "")
        rows.append({
            "run": run, "slug": sol_path.parent.parent.name, "task": sol_path.parent.name, "sol": sol,
            "model": next((s.get("model_name") for s in steps if s.get("model_name")), sol_path.parent.parent.name),
            "host": re.sub(r"^https?://([^/]+).*", r"\1", url) if url else "?",
            "passed": (verdict.group(1) == "PASSED") if verdict else None,
        })
    return rows


def runs(prefix: str) -> List[str]:
    return sorted(p.name for p in BENCH.glob(prefix) if p.is_dir())


# --------------------------------------------------------------------------- per-run metrics
def patch_files(patch: str) -> List[str]:
    return re.findall(r"^diff --git a/(\S+)", patch or "", re.M)


def first_touch(sol: dict, edit: bool) -> Optional[int]:
    """First step that reads (any mention) / edits (edit_file or a write) a file of the final patch."""
    files = patch_files(sol.get("solution", ""))
    for st in sol.get("steps", []):
        code = st.get("sandbox_input", "")
        for f in files:
            name = f.split("/")[-1]
            if edit and re.search(r"edit_file\([^)]*" + re.escape(name), code):
                return st["step"]
            if not edit and (f in code or name in code):
                return st["step"]
    return None


def summary_counts(out: str):
    m = re.search(r"summary: (\d+) passed, (\d+) failed", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def failures_first_decrease(sol: dict) -> Optional[int]:
    """Step at which the number of failing tests first drops below the first run_tests result (the baseline)."""
    baseline = None
    for st in sol.get("steps", []):
        c = summary_counts(st.get("sandbox_output", ""))
        if c is None:
            continue
        if baseline is None:
            baseline = c[1]
        elif c[1] < baseline:
            return st["step"]
    return None


def tests_first_pass(sol: dict) -> Optional[int]:
    for st in sol.get("steps", []):
        c = summary_counts(st.get("sandbox_output", ""))
        if c and c[0] > 0 and c[1] == 0:
            return st["step"]
    return None


def submitted_by(sol: dict) -> str:
    err = sol.get("error") or ""
    if "submitted" in err or "fallback" in err:
        return "fallback"
    if sol.get("success"):
        return "final_answer"
    if err.startswith("LLM failure"):
        return "llm-failure"
    return "none"


def gap(sol: dict) -> Optional[int]:
    tp = tests_first_pass(sol)
    return sol["iterations"] - tp if tp is not None and submitted_by(sol) == "final_answer" else None


def cost(model: str, sol: dict) -> Optional[float]:
    p = PRICES.get(model)
    if not p:
        return None
    return (sol["total_input_tokens"] * p["prompt_per_M"] + sol["total_output_tokens"] * p["completion_per_M"]) / 1e6


def wilson(k: int, n: int) -> tuple:
    if n == 0:
        return (0.0, 0.0)
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def short(model: str) -> str:
    return model.replace("nvidia/", "").replace("-30b-a3b", "").replace(":free", " (OR)")


# --------------------------------------------------------------------------- SVG charts (no dependencies)
def bar_chart(path: Path, title: str, labels: List[str], values: List[float], fmt: str = "{:.0f}",
              errors: Optional[List[tuple]] = None, vmax: Optional[float] = None, unit: str = "") -> None:
    w, row, left, top = 760, 26, 260, 40
    h = top + row * len(labels) + 20
    vmax = vmax or max(values + [1e-9])
    scale = (w - left - 90) / vmax
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="sans-serif" font-size="12">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="10" y="22" font-size="15" font-weight="bold">{title}</text>']
    for i, (lab, v) in enumerate(zip(labels, values)):
        y = top + i * row
        parts.append(f'<text x="{left - 8}" y="{y + 16}" text-anchor="end">{lab}</text>')
        parts.append(f'<rect x="{left}" y="{y + 4}" width="{max(1, v * scale):.1f}" height="{row - 8}" fill="#4878cf"/>')
        if errors:
            lo, hi = errors[i]
            parts.append(f'<line x1="{left + lo * scale:.1f}" x2="{left + hi * scale:.1f}" y1="{y + row / 2}" '
                         f'y2="{y + row / 2}" stroke="#222" stroke-width="1.5"/>')
        parts.append(f'<text x="{left + v * scale + 6:.1f}" y="{y + 16}">{fmt.format(v)}{unit}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts))


def scatter(path: Path, title: str, points: List[tuple]) -> None:
    """points: (label, x = input tokens per task, y = pass rate)."""
    w, h, pad = 760, 420, 60
    xmax = max([p[1] for p in points] + [1]) * 1.1
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="sans-serif" font-size="11">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="10" y="22" font-size="15" font-weight="bold">{title}</text>',
             f'<line x1="{pad}" y1="{h - pad}" x2="{w - 20}" y2="{h - pad}" stroke="#444"/>',
             f'<line x1="{pad}" y1="{h - pad}" x2="{pad}" y2="40" stroke="#444"/>',
             f'<text x="{w / 2}" y="{h - 20}" text-anchor="middle">mean input tokens per task</text>',
             f'<text x="16" y="{h / 2}" transform="rotate(-90 16 {h / 2})" text-anchor="middle">pass rate</text>']
    for frac in (0, 0.5, 1):
        y = h - pad - frac * (h - pad - 50)
        parts.append(f'<text x="{pad - 6}" y="{y + 4}" text-anchor="end">{int(frac * 100)}%</text>')
    for label, x, y in points:
        cx = pad + x / xmax * (w - pad - 30)
        cy = h - pad - y * (h - pad - 50)
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="#d65f5f"/>')
        parts.append(f'<text x="{cx + 7:.1f}" y="{cy - 6:.1f}">{label}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts))


# --------------------------------------------------------------------------- report
def table(header: List[str], rows: List[List]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join("–" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def per_model(rows: List[dict]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for r in rows:
        out.setdefault(r["model"], []).append(r)
    return out


def main() -> None:
    CHARTS.mkdir(parents=True, exist_ok=True)
    main_rows = [r for r in load("v2") if r["passed"] is not None]
    tasks = sorted({r["task"] for r in main_rows})
    models = per_model(main_rows)
    order = sorted(models, key=lambda m: (-sum(r["passed"] for r in models[m]),
                                          mean(r["sol"]["total_input_tokens"] for r in models[m])))
    md: List[str] = []

    md.append(f"## 2. Results ({len(models)} models × {len(tasks)} tasks, {len(main_rows)} runs)\n")
    md.append(table(["Model", "Provider", "Task", "Moulinette", "Agent `success`", "Submitted by", "Iterations",
                     "Input tokens", "Output tokens", "Time (s)"],
                    [[short(r["model"]), r["host"], r["task"], "PASS" if r["passed"] else "FAIL",
                      r["sol"]["success"], submitted_by(r["sol"]), r["sol"]["iterations"],
                      f'{r["sol"]["total_input_tokens"]:,}', f'{r["sol"]["total_output_tokens"]:,}',
                      f'{r["sol"]["total_time_seconds"]:.0f}'] for m in order for r in models[m]]))
    md.append("\n*Moulinette* = `Overall` of `moulinette_eval validate` (the grade). *Agent `success`* = the agent "
              "called `final_answer` with a patch; when it ran out of iterations or the provider stopped answering, "
              "the best patch so far is still submitted (*Submitted by: fallback*), so a run can be `success: false` "
              "and still PASS, or `success: true` and FAIL (a wrong fix).\n")

    md.append("\n**Per model** (pass rate with a 95 % Wilson interval: with 8 tasks one run more or less moves it a lot)\n")
    summary = []
    for m in order:
        rs = models[m]
        k, n = sum(r["passed"] for r in rs), len(rs)
        lo, hi = wilson(k, n)
        solved = [r for r in rs if r["passed"]]
        c = [cost(m, r["sol"]) for r in rs]
        summary.append([short(m), rs[0]["host"], f"**{k}/{n}**", f"{lo:.0%}–{hi:.0%}",
                        f'{mean(r["sol"]["iterations"] for r in rs):.1f}',
                        f'{mean(r["sol"]["total_input_tokens"] for r in rs):,.0f}',
                        f'{mean(r["sol"]["total_output_tokens"] for r in rs):,.0f}',
                        f'{median(r["sol"]["total_time_seconds"] for r in rs):.0f}',
                        f'{sum(r["sol"]["total_input_tokens"] for r in rs) / max(1, len(solved)):,.0f}',
                        f"${sum(c) / max(1, len(solved)):.4f}" if None not in c else "n/a"])
    md.append(table(["Model", "Provider", "Solved", "95 % CI", "Avg iterations", "Avg input tok", "Avg output tok",
                     "Median time (s)", "Input tok per solved task", "Paid-equivalent $ per solved task"], summary))
    bar_chart(CHARTS / "pass_rate.svg", f"SWE-bench pass rate ({len(tasks)} tasks, moulinette verdict, 95% CI)",
              [short(m) for m in order], [sum(r["passed"] for r in models[m]) / len(models[m]) for m in order],
              fmt="{:.0%}", errors=[wilson(sum(r["passed"] for r in models[m]), len(models[m])) for m in order], vmax=1)
    scatter(CHARTS / "tokens_vs_pass.svg", "Cost (input tokens) vs pass rate",
            [(short(m), mean(r["sol"]["total_input_tokens"] for r in models[m]),
              sum(r["passed"] for r in models[m]) / len(models[m])) for m in order])
    md.append("\n![pass rate](benchmarks/swebench/charts/pass_rate.svg)\n\n"
              "![tokens vs pass rate](benchmarks/swebench/charts/tokens_vs_pass.svg)\n")

    by_task = []
    for t in tasks:
        rs = [r for r in main_rows if r["task"] == t]
        by_task.append([t, f'{sum(r["passed"] for r in rs)}/{len(rs)}',
                        f'{mean(r["sol"]["iterations"] for r in rs):.1f}',
                        f'{mean(r["sol"]["total_input_tokens"] for r in rs):,.0f}'])
    md.append("\n**Per task** (how hard each task is across all models)\n")
    md.append(table(["Task", "Models that solve it", "Avg iterations", "Avg input tokens"], by_task))

    # ---- reliability
    md.append("\n## 3. Provider reliability\n")
    rel = []
    for m in order:
        rs = models[m]
        times = [st["request_time_ms"] for r in rs for st in r["sol"]["steps"] if st.get("request_time_ms")]
        reqs = sum(r["sol"]["total_requests"] for r in rs)
        retries = sum(st.get("retries", 0) for r in rs for st in r["sol"]["steps"])
        dead = sum(1 for r in rs if submitted_by(r["sol"]) == "llm-failure"
                   or (r["sol"].get("error") or "").startswith("LLM failure"))
        rel.append([short(m), rs[0]["host"], reqs, f"{mean(times):,.0f}" if times else None,
                    f"{sorted(times)[int(0.95 * (len(times) - 1))]:,.0f}" if times else None,
                    retries, f"{retries / max(1, reqs):.2f}", f"{dead}/{len(rs)}", f"{1 - dead / len(rs):.0%}"])
    md.append(table(["Model", "Provider", "Requests", "Avg response (ms)", "p95 response (ms)", "Retries",
                     "Retries per request", "Runs cut by the provider", "Availability"], rel))
    lat = [(short(m), mean([st["request_time_ms"] for r in models[m] for st in r["sol"]["steps"]
                            if st.get("request_time_ms")] or [0])) for m in order]
    bar_chart(CHARTS / "latency.svg", "Average response time per request (ms)", [x[0] for x in lat],
              [x[1] for x in lat], fmt="{:,.0f}", unit=" ms")
    md.append("\n![latency](benchmarks/swebench/charts/latency.svg)\n")

    # ---- intermediary metrics
    md.append("\n## 4. Intermediary metrics\n")
    inter = []
    for m in order:
        for r in models[m]:
            s = r["sol"]
            inter.append([short(m), r["task"], "PASS" if r["passed"] else "FAIL", first_touch(s, False),
                          first_touch(s, True), failures_first_decrease(s), tests_first_pass(s), gap(s)])
    md.append(table(["Model", "Task", "Moulinette", "First read of a patched file", "First edit of it",
                     "Failures first decrease", "Tests first pass", "Steps from first pass to final_answer"], inter))
    agg = []
    for m in order:
        rs = models[m]
        def avg(f):
            v = [f(r["sol"]) for r in rs]
            v = [x for x in v if x is not None]
            return f"{mean(v):.1f} ({len(v)})" if v else None
        agg.append([short(m), avg(lambda s: first_touch(s, False)), avg(lambda s: first_touch(s, True)),
                    avg(tests_first_pass), avg(gap),
                    f'{sum(1 for r in rs if submitted_by(r["sol"]) == "fallback")}/{len(rs)}'])
    md.append("\n**Averages per model** (in brackets: number of runs where the event happened)\n")
    md.append(table(["Model", "First read", "First edit", "Tests first pass", "Pass → final_answer gap",
                     "Runs submitted by fallback"], agg))

    # ---- variance of the reference model
    reps = [("v2", load("v2"))] + [(r, load(r)) for r in runs("v2-repeat-*")]
    reps = [(name, [r for r in rows if r["slug"] == REFERENCE and r["passed"] is not None]) for name, rows in reps]
    reps = [(n, rs) for n, rs in reps if rs]
    if len(reps) > 1:
        md.append(f"\n## 5. Run-to-run variance ({short(REFERENCE.replace('nvidia_', 'nvidia/'))}, "
                  f"{len(reps)} identical runs of the {len(tasks)} tasks)\n")
        rows_v = []
        for t in tasks:
            got = [next((r for r in rs if r["task"] == t), None) for _, rs in reps]
            got = [g for g in got if g]
            rows_v.append([t, f'{sum(g["passed"] for g in got)}/{len(got)}',
                           " / ".join(str(g["sol"]["iterations"]) for g in got),
                           " / ".join(f'{g["sol"]["total_input_tokens"] // 1000}k' for g in got)])
        md.append(table(["Task", "Passed (runs)", "Iterations per run", "Input tokens per run"], rows_v))
        totals = [sum(r["passed"] for r in rs) for _, rs in reps]
        toks = [mean(r["sol"]["total_input_tokens"] for r in rs) for _, rs in reps]
        md.append(f"\nSolved per run: {', '.join(map(str, totals))} of {len(tasks)} — mean {mean(totals):.2f}, "
                  f"standard deviation {pstdev(totals):.2f}. Mean input tokens per task per run: "
                  f"{', '.join(f'{t:,.0f}' for t in toks)} (sd {pstdev(toks):,.0f}). A task is *reliably* solved "
                  f"when it passes in every run: {sum(1 for r in rows_v if r[1].split('/')[0] == r[1].split('/')[1])}"
                  f"/{len(tasks)}.\n")

    # ---- ablations
    abl = runs("v2-ablation-*")
    if abl:
        md.append("\n## 6. Ablations (reference model, same tasks, one mechanism off)\n")
        base = [r for _, rs in reps for r in rs] if reps else []
        def stats(rs: List[dict], n_runs: int = 1):
            return [f'{sum(r["passed"] for r in rs) / n_runs:.1f}/{len(tasks)}' if n_runs > 1 else
                    f'{sum(r["passed"] for r in rs)}/{len(rs)}',
                    f'{mean(r["sol"]["iterations"] for r in rs):.1f}',
                    f'{mean(r["sol"]["total_input_tokens"] for r in rs):,.0f}',
                    f'{mean(r["sol"]["total_output_tokens"] for r in rs):,.0f}',
                    f'{mean(r["sol"]["total_time_seconds"] for r in rs):.0f}',
                    f'{sum(1 for r in rs if submitted_by(r["sol"]) == "fallback")}']
        lines = [["baseline (mean of the identical runs)" if len(reps) > 1 else "baseline", *stats(base, len(reps))]]
        bars = [("baseline", sum(r["passed"] for r in base) / max(1, len(reps)))]
        for name in abl:
            rs = [r for r in load(name) if r["passed"] is not None]
            if rs:
                label = name.replace("v2-ablation-", "")
                lines.append([label, *stats(rs)])
                bars.append((label, sum(r["passed"] for r in rs)))
        md.append(table(["Variant", "Solved", "Avg iterations", "Avg input tok", "Avg output tok", "Avg time (s)",
                         "Fallback submissions"], lines))
        bar_chart(CHARTS / "ablations.svg", f"Ablations: tasks solved (of {len(tasks)})", [b[0] for b in bars],
                  [b[1] for b in bars], fmt="{:.1f}", vmax=len(tasks))
        md.append("\n![ablations](benchmarks/swebench/charts/ablations.svg)\n")
        md.append("\nPer task:\n")
        per = []
        for t in tasks:
            cells = [t, f'{sum(r["passed"] for r in base if r["task"] == t)}/{len(reps)}']
            for name in abl:
                r = next((x for x in load(name) if x["task"] == t), None)
                cells.append(None if r is None or r["passed"] is None else ("PASS" if r["passed"] else "FAIL"))
            per.append(cells)
        md.append(table(["Task", "baseline (runs passed)"] + [a.replace("v2-ablation-", "") for a in abl], per))

    # ---- cost
    md.append("\n## 7. Cost analysis\n")
    md.append("Every run above was free (free tiers / `:free` models). Two costs still matter: the **free quota** "
              "a task consumes, and what the same tokens would cost on the **paid** version of the model "
              "(public OpenRouter prices of 2026-11-21, `benchmarks/swebench/paid-equivalent-prices.json`).\n")
    crow = []
    for m in order:
        rs = models[m]
        host = rs[0]["host"]
        q_text, per_day = next(((v[0], v[1]) for k, v in QUOTA.items() if k in host), ("?", None))
        req_task = mean(r["sol"]["total_requests"] for r in rs)
        c = [cost(m, r["sol"]) for r in rs]
        solved = max(1, sum(r["passed"] for r in rs))
        crow.append([short(m), f"{req_task:.1f}", q_text,
                     f"{per_day / (3 * req_task):.0f}" if per_day else "not limiting",
                     f"${sum(c) / len(rs):.4f}" if None not in c else "n/a",
                     f"${sum(c) / solved:.4f}" if None not in c else "n/a"])
    md.append(table(["Model", "Requests per task", "Free quota", "Full SWE exams (3 tasks) per day of quota",
                     "Paid-equivalent $ per task", "Paid-equivalent $ per solved task"], crow))
    print("\n".join(md))


if __name__ == "__main__":
    sys.exit(main())
