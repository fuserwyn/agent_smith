/* Stage 5 scenarios: welcome screen, command palette, settings, focus shortcuts. */
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { TOKEN } from "../playwright.config";

const SHOTS = process.env.STUDIO_SHOTS;
async function open(page: Page) {
  await page.goto(`/?token=${TOKEN}`);
  await expect(page.locator(".conn.connected")).toBeVisible();
}
async function command(page: Page, text: string) {
  await page.keyboard.press("ControlOrMeta+K");
  await page.getByRole("dialog").getByRole("textbox").fill(text);
  if (SHOTS && text === "light") await page.screenshot({ path: join(SHOTS, "12-palette.png") });
  await page.keyboard.press("Enter");
}

test("welcome screen with the main shortcuts", async ({ page }) => {
  await open(page);
  await expect(page.locator(".welcome")).toContainText("Agent Smith Studio");
  await expect(page.locator(".welcome kbd")).toHaveCount(5);
});

test("the command palette switches the theme and opens views", async ({ page }) => {
  await open(page);
  await command(page, "light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await command(page, "dark theme");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await command(page, "settings");
  await expect(page.locator(".settings")).toContainText(/keyboard shortcuts|горячие клавиши/i);
  if (SHOTS) await page.screenshot({ path: join(SHOTS, "13-settings.png") });
  await command(page, "agent runs");
  await expect(page.locator(".sidepanel .pane-title")).toContainText(/agent runs|запуски агента/i);
});

test("new terminal from the palette, chat focus with Cmd/Ctrl+L", async ({ page }) => {
  await open(page);
  await page.getByRole("tab", { name: /terminal|терминал/i }).first().click();
  await expect(page.locator(".term-tab").first()).toBeVisible();
  const before = await page.locator(".term-tab").count();
  await command(page, "new terminal");
  await expect(page.locator(".term-tab")).toHaveCount(before + 1);
  await page.keyboard.press("ControlOrMeta+L");
  await expect(page.locator(".composer-in")).toBeFocused();
});

test("no Content Security Policy violation while using the editor, terminal and observatory", async ({ page }) => {
  const violations: string[] = [];
  page.on("console", (m) => { if (/Content Security Policy|Refused to/i.test(m.text())) violations.push(m.text()); });
  page.on("pageerror", (e) => violations.push(String(e)));
  await open(page);
  await page.keyboard.press("ControlOrMeta+P");
  await page.getByRole("dialog").getByRole("textbox").fill("calc");
  await expect(page.getByRole("option", { name: /calc\.py/ })).toBeVisible();  // the file index loads asynchronously
  await page.keyboard.press("Enter");
  await expect(page.locator(".monaco-host").first().locator(".view-lines")).toContainText("def add");
  await page.getByRole("tab", { name: /terminal|терминал/i }).first().click();
  await expect(page.locator(".term-host:visible .xterm-rows")).toBeVisible();
  await page.getByRole("tab", { name: /inside the agent|внутри агента/i }).first().click();
  await expect(page.frameLocator(".inside-frame").locator("#diagram .node").first()).toBeVisible();
  expect(violations).toEqual([]);
});

test("typing / in the chat lists the commands", async ({ page }) => {
  await open(page);
  await page.locator(".composer-in").fill("/");
  await expect(page.locator(".cmd-item")).toHaveCount(2);
  await page.locator(".composer-in").fill("/sw");
  await expect(page.locator(".cmd-item")).toHaveCount(1);
  await page.locator(".composer-in").press("Tab");
  await expect(page.locator(".composer-in")).toHaveValue("/swe ");
});
