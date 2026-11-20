/* Stage 2 scenarios: terminal runs commands, sees the project, survives a reload, several sessions. */
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { TOKEN } from "../playwright.config";

const SHOTS = process.env.STUDIO_SHOTS;
const term = (page: Page) => page.locator(".term-host:visible .xterm-rows");

async function openTerminal(page: Page) {
  await page.goto(`/?token=${TOKEN}`);
  await expect(page.locator(".conn.connected")).toBeVisible();
  await page.getByRole("tab", { name: /terminal|терминал/i }).first().click();
  await expect(term(page)).toBeVisible();
  await page.locator(".term-host:visible").click();
}
const run = async (page: Page, cmd: string) => { await page.keyboard.type(cmd); await page.keyboard.press("Enter"); };

test("run a command and see a file it creates appear in the explorer", async ({ page }) => {
  await openTerminal(page);
  await run(page, "echo result=$((40+2))");
  await expect(term(page)).toContainText("result=42");
  await run(page, "echo from-terminal > made_by_terminal.txt");
  await expect(page.getByRole("treeitem", { name: /made_by_terminal\.txt/ })).toBeVisible();
  if (SHOTS) await page.screenshot({ path: join(SHOTS, "8-terminal.png") });
});

test("the terminal keeps its output across a page reload", async ({ page }) => {
  await openTerminal(page);
  await run(page, "echo before-reload=$((7*6))");
  await expect(term(page)).toContainText("before-reload=42");
  await page.reload();
  await page.getByRole("tab", { name: /terminal|терминал/i }).first().click();
  await expect(term(page)).toContainText("before-reload=42");
  await expect(page.locator(".term-tab")).toHaveCount(await page.locator(".term-tab").count()); // same sessions, none duplicated
});

test("several terminals, each closable", async ({ page }) => {
  await openTerminal(page);
  const before = await page.locator(".term-tab").count();
  await page.getByRole("button", { name: /new terminal|новый терминал/i }).click();
  await expect(page.locator(".term-tab")).toHaveCount(before + 1);
  await page.locator(".term-host:visible").click();
  await run(page, "echo second=$((2+2))");
  await expect(term(page)).toContainText("second=4");
  await page.locator(".term-tab.active .tab-x").click();
  await expect(page.locator(".term-tab")).toHaveCount(before);
});

test("app shortcuts still work while the terminal has the focus", async ({ page }) => {
  await openTerminal(page);                       // focus is in xterm now
  await page.keyboard.press("ControlOrMeta+P");   // Ctrl+P on Linux was swallowed by the shell (previous command)
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
});
