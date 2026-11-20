"""Which model is worth it in the studio? Same real task for every model, scored by the tests, priced by OpenRouter.

    uv run --extra studio python scripts/eval_studio_models.py openrouter:anthropic/claude-haiku-5.5 [more …] [--runs 2]

The task: a small shop package with two bugs in two files and failing unit tests; the studio agent (Agent mode,
local shell, no fallback) must fix the code without touching the tests. A run passes only if the tests pass
afterwards and the test file is unchanged. Cost = tokens x the model's OpenRouter price (public catalog).
Results: benchmarks/studio/models.json (+ a Markdown table on stdout). Costs real money with paid models.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["AGENT_FALLBACK"] = "0"           # measure the model itself, never a fallback
os.environ["AGENT_SMITH_CODE_DIR"] = "off"

FILES = {
    "shop/__init__.py": "",
    "shop/discounts.py": (
        "TIERS = {\"gold\": 0.2, \"silver\": 0.1}\n\n\n"
        "def discount_for(customer, subtotal):\n"
        "    \"\"\"Fraction of the subtotal taken off: the tier rate, +0.05 from 100 (included) upwards.\"\"\"\n"
        "    rate = TIERS.get(customer.get(\"tier\"), 0)\n"
        "    if subtotal > 100:\n"
        "        rate += 0.05\n"
        "    return rate\n"),
    "shop/pricing.py": (
        "from shop.discounts import discount_for\n\n\n"
        "def total(items, customer):\n"
        "    \"\"\"Price of the order after the customer's discount, rounded to cents.\"\"\"\n"
        "    subtotal = sum(i[\"price\"] for i in items)\n"
        "    return round(subtotal * (1 - discount_for(customer, subtotal)), 2)\n"),
    "tests/__init__.py": "",
    "tests/test_pricing.py": (
        "import unittest\n\nfrom shop.pricing import total\n\n\n"
        "class TotalTest(unittest.TestCase):\n"
        "    def test_no_discount(self):\n"
        "        self.assertEqual(total([{\"price\": 10, \"qty\": 3}], {}), 30)\n\n"
        "    def test_tier(self):\n"
        "        self.assertEqual(total([{\"price\": 25, \"qty\": 2}], {\"tier\": \"silver\"}), 45)\n\n"
        "    def test_bulk_from_100_included(self):\n"
        "        self.assertEqual(total([{\"price\": 50, \"qty\": 2}], {}), 95)\n\n"
        "    def test_tier_and_bulk(self):\n"
        "        self.assertEqual(total([{\"price\": 40, \"qty\": 5}], {\"tier\": \"gold\"}), 150)\n\n\n"
        "if __name__ == \"__main__\":\n    unittest.main()\n"),
    "README.md": "# shop\n\nRun the tests with `python3 -m unittest`.\n",
}
TASK = ("The unit tests fail (run them with: python3 -m unittest). Find and fix the bugs in the shop package — "
        "do not modify the tests — then run the tests to confirm they pass.")


def tests_pass(root: Path) -> bool:
    r = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=root, capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def prices() -> dict:
    import httpx  # certifi's CA bundle (urllib can miss the system one on macOS)
    data = httpx.get("https://openrouter.ai/api/v1/models", timeout=30).json()["data"]  # public catalog
    return {m["id"]: m["pricing"] for m in data}


def one(spec: str, attempt: int, price: dict) -> dict:
    from fastapi.testclient import TestClient
    from agent_smith.studio.app import create_app
    provider, model = spec.split(":", 1)
    tmp = Path(tempfile.mkdtemp(prefix="studio-eval-"))
    proj = tmp / "proj"
    for rel, text in FILES.items():
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=proj)
    assert not tests_pass(proj)
    app = create_app(proj, "eval-token", web_dist=None, shell="local", state_dir=tmp / "state")
    h = {"Authorization": "Bearer eval-token", "X-Studio": "1"}
    t0 = time.monotonic()
    with TestClient(app, base_url="http://127.0.0.1:8790") as client:
        r = client.post("/api/agent/runs", json={"message": TASK, "mode": "agent", "provider": provider, "model": model}, headers=h)
        r.raise_for_status()
        rid = r.json()["id"]
        while True:
            run = client.get("/api/agent/state", headers=h).json()["runs"][rid]
            if run["status"] != "running" or time.monotonic() - t0 > 900:
                break
            time.sleep(1)
    ok = tests_pass(proj) and (proj / "tests/test_pricing.py").read_text() == FILES["tests/test_pricing.py"]
    p = price.get(model, {})
    cost = run["input_tokens"] * float(p.get("prompt") or 0) + run["output_tokens"] * float(p.get("completion") or 0)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"model": model, "attempt": attempt, "pass": ok, "status": run["status"], "steps": run["steps"],
            "input_tokens": run["input_tokens"], "output_tokens": run["output_tokens"],
            "seconds": round(time.monotonic() - t0, 1), "cost_usd": round(cost, 4), "error": run.get("error")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+", help="provider:model, e.g. openrouter:anthropic/claude-haiku-5.5")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()
    price = prices()
    jobs = [(m, i) for m in a.models for i in range(1, a.runs + 1)]
    results = []
    with cf.ProcessPoolExecutor(max_workers=a.jobs) as ex:
        for res in ex.map(one, *zip(*jobs), [price] * len(jobs)):
            results.append(res)
            print(json.dumps(res), flush=True)
    out = ROOT / "benchmarks" / "studio" / "models.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "task": TASK, "runs": results}, indent=1) + "\n")
    print("\n| Model | Passed | Avg steps | Avg time (s) | Avg cost ($) |\n|---|---|---|---|---|")
    for m in dict.fromkeys(r["model"] for r in results):
        rs = [r for r in results if r["model"] == m]
        avg = lambda k: sum(r[k] for r in rs) / len(rs)  # noqa: E731
        print(f"| {m} | {sum(r['pass'] for r in rs)}/{len(rs)} | {avg('steps'):.1f} | {avg('seconds'):.0f} | {avg('cost_usd'):.4f} |")


if __name__ == "__main__":
    main()
