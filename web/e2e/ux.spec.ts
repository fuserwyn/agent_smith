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
  await expect(page.locator(".run-row").first()).toBeVisible();               // the agent test made a run
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
