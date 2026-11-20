/* End-to-end tests: a real agent-studio server on a throwaway project (with git history), driven in Chrome. */
import { execSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineConfig } from "@playwright/test";

export const PORT = 8799;
export const LLM_PORT = 8798;
export const TOKEN = "e2e-token";

// fixture project, created once per run (the config is evaluated before the server starts)
const root = process.env.STUDIO_E2E_ROOT ?? mkdtempSync(join(tmpdir(), "studio-e2e-"));
if (!process.env.STUDIO_E2E_ROOT) {
  process.env.STUDIO_E2E_ROOT = root;
  const w = (p: string, s: string) => { mkdirSync(join(root, p, ".."), { recursive: true }); writeFileSync(join(root, p), s); };
  w("src/calc.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n");
  w("src/util/strings.py", "def shout(s):\n    return s.upper() + '!'\n");
  w("README.md", "# Demo project\n\nA tiny project for the studio tests. needle-in-readme\n");
  w(".env", "API_KEY=super-secret-value\n");
  const git = (c: string) => execSync(`git ${c}`, { cwd: root, stdio: "ignore", env: { ...process.env, GIT_AUTHOR_NAME: "e2e", GIT_AUTHOR_EMAIL: "e2e@x", GIT_COMMITTER_NAME: "e2e", GIT_COMMITTER_EMAIL: "e2e@x" } });
  git("init -q -b main"); git("add src README.md"); git("commit -q -m init");
  // the agent talks to a scripted, OpenAI-compatible fake model (tests/fake_llm_server.py)
  writeFileSync(join(root, "..", "studio-e2e-models.json"), JSON.stringify({
    providers: [{ name: "e2e", base_url: `http://127.0.0.1:${LLM_PORT}/v1`, key_env: null, hosts: ["127.0.0.1"] }],
    models: [{ provider: "e2e", model: "scripted-model" }], fallbacks: [], fallback_enabled: false,
  }));
  w("src/calc.py", "def add(a, b):\n    return a + b  # changed\n\n\ndef sub(a, b):\n    return a - b\n");
}

export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: process.env.CI ? undefined : "chrome", // local: the installed Chrome, no browser download
    viewport: { width: 1440, height: 900 },
    colorScheme: "dark",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `uv run --project .. python ../tests/fake_llm_server.py --port ${LLM_PORT} --script e2e/fixtures/agent-script.json --loop`,
      port: LLM_PORT,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      // local shell in tests (fast, no image pull); the Docker shell has its own test (STUDIO_DOCKER_TESTS=1)
      command: `uv run --project .. --extra studio agent-studio "${root}" --port ${PORT} --no-browser --shell ${process.env.STUDIO_E2E_SHELL ?? "local"}`,
      env: { STUDIO_TOKEN: TOKEN, AGENT_SMITH_MODELS_FILE: join(root, "..", "studio-e2e-models.json"), XDG_CACHE_HOME: join(root, "..", "studio-e2e-cache") },
      url: `http://127.0.0.1:${PORT}/`,
      reuseExistingServer: false,
      timeout: 90_000,
    },
  ],
});
