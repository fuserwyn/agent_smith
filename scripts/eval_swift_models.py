"""Which model writes Apple apps? One single-file SwiftUI + SwiftData app per model, typechecked with Xcode against
the macOS 15 and iOS 18 SDKs; when it fails, the compiler errors go back once for a fix. Paid OpenRouter calls.

    uv run python scripts/eval_swift_models.py OUT_DIR anthropic/claude-haiku-5.5,openai/gpt-6.2-sol [runs] > results.jsonl

Needs Xcode (macOS). Results of 2026-11-21: benchmarks/studio/swift.json."""
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
from agent_smith.llm.client import build_client  # noqa: E402

OUT = Path(sys.argv[1])
MODELS = sys.argv[2].split(",")
RUNS = int(sys.argv[3]) if len(sys.argv) > 3 else 2
FIX = True
PROMPT = """Write a complete single-file SwiftUI app that builds for BOTH macOS 15 and iOS 18 from the same file.

Requirements:
- SwiftData model `Note` (title, body, createdAt, isPinned) and `.modelContainer(for: Note.self)` on the WindowGroup.
- `@main` App struct.
- macOS: `NavigationSplitView` (sidebar list + detail). iOS: `NavigationStack` with navigation to the detail. Use `#if os(macOS)` / `#else`.
- List sorted with pinned notes first, then newest; `.searchable` filtering by title or body.
- Add, delete (swipe on iOS, context menu on both), pin/unpin; the detail edits title and body in place.
- An `@Observable` class `QuoteService` with `func load() async` fetching JSON from https://api.example.com/quote
  ({"text": String, "author": String}) with URLSession async/await and Codable; show the quote in the sidebar header,
  with a loading state and an error message.
- No third-party packages, no placeholders, no TODOs.

Answer with ONE ```swift code block containing the whole file, nothing else."""
DEV = {**os.environ, "DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer"}


def sdk(name):
    return subprocess.run(["xcrun", "--sdk", name, "--show-sdk-path"], capture_output=True, text=True, env=DEV).stdout.strip()


SDKS = {"macOS": (sdk("macosx"), "arm64-apple-macos15"), "iOS": (sdk("iphonesimulator"), "arm64-apple-ios18.0-simulator")}
PRICES = {}


def prices():
    import httpx
    for m in httpx.get("https://openrouter.ai/api/v1/models", timeout=20).json()["data"]:
        PRICES[m["id"]] = (float(m["pricing"]["prompt"]), float(m["pricing"]["completion"]))


def one(model, attempt):
    t0 = time.monotonic()
    llm = build_client(model, "https://openrouter.ai/api/v1", no_fallbacks=True)
    try:
        r = llm.chat([{"role": "user", "content": PROMPT}], max_tokens=16000, temperature=0.2)
    except BaseException as e:  # noqa: BLE001
        return {"model": model, "attempt": attempt, "ok": False, "error": str(e)[:200]}
    tin, tout = r.input_tokens, r.output_tokens
    f = OUT / f"{model.replace('/', '_')}_{attempt}.swift"

    def check(text):
        code = re.findall(r"```swift\s*\n(.*?)```", text, re.S)
        code = max(code, key=len) if code else ""
        f.write_text(code)
        errs, log = {}, ""
        for name, (path, target) in SDKS.items():
            p = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-typecheck", "-sdk", path, "-target", target, str(f)],
                               capture_output=True, text=True, env=DEV, timeout=300)
            errs[name] = len(re.findall(r": error:", p.stdout + p.stderr)) if p.returncode else 0
            if p.returncode:
                log += f"--- {name} ---\n" + "\n".join(l for l in (p.stdout + p.stderr).splitlines() if ": error:" in l)[:3000] + "\n"
        return code, errs, log

    code, first, log = check(r.text)
    ok1 = bool(code) and not any(first.values())
    ok2, after = ok1, first
    if not ok1 and code and FIX:
        msgs = [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": "```swift\n" + code + "```"},
                {"role": "user", "content": "The Swift compiler (Xcode, macOS 15 and iOS 18 SDKs) reports:\n" + log
                 + "\nFix every error. Answer with the whole corrected file in ONE ```swift block."}]
        try:
            r2 = llm.chat(msgs, max_tokens=16000, temperature=0.2)
            tin, tout = tin + r2.input_tokens, tout + r2.output_tokens
            code, after, _ = check(r2.text)
            ok2 = bool(code) and not any(after.values())
        except BaseException:  # noqa: BLE001
            pass
    pi, po = PRICES.get(model.split("@")[0], (0, 0))
    return {"model": model, "attempt": attempt, "ok": ok1, "ok_after_fix": ok2, "errors": first, "errors_after": after,
            "lines": code.count("\n"), "in": tin, "out": tout,
            "cost": round(tin * pi + tout * po, 4), "s": round(time.monotonic() - t0)}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    prices()
    jobs = [(m, i) for m in MODELS for i in range(1, RUNS + 1)]
    with cf.ThreadPoolExecutor(8) as ex:
        for res in ex.map(lambda j: one(*j), jobs):
            print(json.dumps(res), flush=True)
