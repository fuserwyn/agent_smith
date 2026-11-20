/* README recordings, not a test: captures frames of two short scenes, assembled into GIFs by
 * scripts/make_gif.py. Runs only with STUDIO_GIF=<dir> (see `make studio-gif`). */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { TOKEN } from "../playwright.config";

const OUT = process.env.STUDIO_GIF;
test.skip(!OUT, "recording only (STUDIO_GIF=<dir>)");
test.use({ viewport: { width: 1280, height: 800 } });
test.setTimeout(120_000);

// a visible mouse pointer (screenshots have none); injected by Playwright, so the page CSP does not apply
const POINTER = () => {
  const draw = () => {
    const dot = document.createElement("div");
    dot.id = "demo-pointer";
    Object.assign(dot.style, {
      position: "fixed", left: "0", top: "0", width: "18px", height: "18px", marginLeft: "-9px", marginTop: "-9px",
      borderRadius: "50%", background: "rgba(255,255,255,.85)", border: "2px solid rgba(124,108,255,.95)",
      boxShadow: "0 2px 8px rgba(0,0,0,.45)", zIndex: "2147483647", pointerEvents: "none", transition: "transform .12s",
    });
    document.body.appendChild(dot);
    addEventListener("mousemove", (e) => { dot.style.left = `${e.clientX}px`; dot.style.top = `${e.clientY}px`; }, true);
    addEventListener("mousedown", () => { dot.style.transform = "scale(.7)"; }, true);
    addEventListener("mouseup", () => { dot.style.transform = ""; }, true);
  };
  if (window.top === window) document.readyState === "loading" ? addEventListener("DOMContentLoaded", draw) : draw();
};

function recorder(page: Page, name: string) {
  const dir = join(OUT!, name);
  mkdirSync(dir, { recursive: true });
  const frames: { file: string; t: number }[] = [];
  let on = true;
  const loop = (async () => {
    while (on) {
      const file = join(dir, `${String(frames.length).padStart(4, "0")}.png`);
      const t = Date.now();
      await page.screenshot({ path: file });
      frames.push({ file, t });
      await page.waitForTimeout(60);
    }
  })();
  return async () => {
    on = false;
    await loop;
    writeFileSync(join(dir, "frames.json"), JSON.stringify(frames.map((f) => ({ ...f, t: f.t - frames[0]!.t }))));
  };
}

async function moveClick(page: Page, target: Locator) {
  const b = (await target.boundingBox())!;
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 18 });
  await page.waitForTimeout(150);
  await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
}

test("record the README GIFs", async ({ page }) => {
  await page.addInitScript(POINTER);
  await page.goto(`/?token=${TOKEN}`);
  await expect(page.locator(".conn.connected")).toBeVisible();
  await page.mouse.move(640, 400);

  // scene 1: the agent changes a file; the steps stream in; the change is reviewed as a diff
  let stop = recorder(page, "agent");
  await page.waitForTimeout(600);
  await moveClick(page, page.locator(`[role="treeitem"][data-path="src"]`));
  await moveClick(page, page.locator(`[role="treeitem"][data-path="src/calc.py"]`));
  await expect(page.locator(".monaco-host").first().locator(".view-lines")).toContainText("def sub");
  await moveClick(page, page.locator(".composer-in"));
  await page.locator(".composer-in").pressSequentially("Add a divide function that refuses zero", { delay: 45 });
  await page.waitForTimeout(300);
  await page.keyboard.press("Enter");
  await expect(page.locator(".md")).toContainText("ValueError", { timeout: 60_000 });
  await page.waitForTimeout(1500);
  await moveClick(page, page.locator(".chg-open", { hasText: "src/calc.py" }));
  await expect(page.locator(".monaco-diff-editor .line-insert, .monaco-diff-editor .char-insert").first()).toBeVisible();
  await page.waitForTimeout(2500);
  await stop();

  // scene 2: the same turn replayed inside the agent
  stop = recorder(page, "inside");
  await moveClick(page, page.getByRole("button", { name: /inside the agent/i }).last());
  await moveClick(page, page.getByRole("button", { name: /full screen/i }));
  const obs = page.frameLocator(".inside-frame");
  await expect(obs.locator("#diagram .node").first()).toBeVisible();
  await page.waitForTimeout(800);
  await obs.locator("#speed").selectOption("4");
  await moveClick(page, obs.locator("#b-first"));
  await moveClick(page, obs.locator("#b-play"));
  await expect(obs.locator("#pos")).toHaveText(/^(\d+) \/ \1$/, { timeout: 60_000 });   // played to the end
  await page.waitForTimeout(2000);
  await stop();
});
