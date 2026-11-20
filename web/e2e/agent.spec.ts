/* Stage 3 scenario: ask the agent for a change, watch its steps, review the diff, undo the file. */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import { TOKEN } from "../playwright.config";

const root = () => process.env.STUDIO_E2E_ROOT!;
const SHOTS = process.env.STUDIO_SHOTS;

// TODO(stage 3): the message is not shown after Enter in the browser yet — debug, then remove fixme
test("the agent edits a file, the change is reviewed and undone", async ({ page }) => {
  await page.goto(`/?token=${TOKEN}`);
  await expect(page.locator(".conn.connected")).toBeVisible();
  const box = page.getByRole("textbox", { name: /message to the agent|сообщение агенту/i });
  await box.fill("Rename sub to subtract");
  await page.keyboard.press("Enter");
  await expect(page.locator(".msg-user")).toContainText("Rename sub to subtract");
  await expect(page.locator(".md")).toContainText("subtract", { timeout: 30_000 });   // the final answer
  await expect(page.locator(".tchip", { hasText: "edit_file" })).toBeVisible();      // the steps stay visible
  await expect(page.locator(".changes")).toContainText("src/calc.py");
  expect(readFileSync(join(root(), "src/calc.py"), "utf8")).toContain("def subtract(a, b):");
  if (SHOTS) await page.screenshot({ path: join(SHOTS, "9-agent.png") });
  await page.locator(".chg-open", { hasText: "src/calc.py" }).click();               // review in the editor
  await expect(page.locator(".crumb-tag")).toBeVisible();
  await expect(page.locator(".monaco-diff-editor .line-insert, .monaco-diff-editor .char-insert").first()).toBeVisible();
  if (SHOTS) await page.screenshot({ path: join(SHOTS, "10-agent-diff.png") });
  await page.locator(".chg").getByRole("button", { name: /undo this file|откатить этот файл/i }).click();
  await expect(page.locator(".changes")).toHaveCount(0);
  expect(readFileSync(join(root(), "src/calc.py"), "utf8")).toContain("def sub(a, b):");
});
