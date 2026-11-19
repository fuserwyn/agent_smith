/* Stage 1 scenarios: explorer, editor, save, external changes, quick open, search, git diff, secrets. */
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { TOKEN } from "../playwright.config";

const root = () => process.env.STUDIO_E2E_ROOT!;
const SHOTS = process.env.STUDIO_SHOTS; // optional folder for review screenshots
const shot = async (page: Page, name: string) => { if (SHOTS) await page.screenshot({ path: join(SHOTS, `${name}.png`) }); };

async function openStudio(page: Page) {
  await page.goto(`/?token=${TOKEN}`);
  await expect(page.locator(".conn.connected")).toBeVisible();
  await expect(page.getByRole("tree")).toContainText("src");
}
const editorText = (page: Page) => page.locator(".monaco-host").first().locator(".view-lines");

test("without the session token the studio shows the key screen", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText(/session key|ключ сессии|clé de session/i).first()).toBeVisible();
  await expect(page.locator(".conn.locked")).toBeVisible();
});

test("token is removed from the address bar", async ({ page }) => {
  await openStudio(page);
  expect(page.url()).not.toContain("token=");
});

test("open, edit and save a file", async ({ page }) => {
  await openStudio(page);
  await page.getByRole("treeitem", { name: /src/ }).first().click();
  await page.getByRole("treeitem", { name: /calc\.py/ }).click();
  await expect(editorText(page)).toContainText("def add(a, b):");
  await expect(page.locator(".tab.active.preview")).toContainText("calc.py");  // single click = preview tab
  await page.locator(".monaco-host").first().click();
  await page.keyboard.press("ControlOrMeta+End");
  await page.keyboard.type("\n\ndef mul(a, b):\n    return a * b\n");
  await expect(page.locator(".tab.active.dirty")).toBeVisible();
  await shot(page, "1-editing");
  await page.keyboard.press("ControlOrMeta+S");
  await expect(page.locator(".toast.ok")).toContainText("calc.py");
  await expect(page.locator(".tab.active.dirty")).toHaveCount(0);
  expect(readFileSync(join(root(), "src/calc.py"), "utf8")).toContain("def mul(a, b):");
});

test("a change on disk updates a clean file and warns on a dirty one", async ({ page }) => {
  await openStudio(page);
  await page.getByRole("treeitem", { name: /README\.md/ }).dblclick();
  await expect(editorText(page)).toContainText("Demo project");
  writeFileSync(join(root(), "README.md"), "# Demo project\n\nEdited by the agent.\n");
  await expect(editorText(page)).toContainText("Edited by the agent.");           // clean: follows the disk
  await page.locator(".monaco-host").first().click();
  await page.keyboard.type("my local edit ");
  writeFileSync(join(root(), "README.md"), "# Demo project\n\nEdited again by the agent.\n");
  await expect(page.locator(".banner.warn")).toBeVisible();                      // dirty: banner, nothing lost
  await shot(page, "2-changed-on-disk");
  await page.locator(".banner.warn").getByRole("button").last().click();          // take the disk version
  await expect(editorText(page)).toContainText("Edited again by the agent.");
  await expect(page.locator(".banner.warn")).toHaveCount(0);
});

test("quick open finds a file by fuzzy name", async ({ page }) => {
  await openStudio(page);
  await page.keyboard.press("ControlOrMeta+P");
  await page.getByRole("dialog").getByRole("textbox").fill("strs");
  await expect(page.locator(".pal-item.sel")).toContainText("strings.py");
  await shot(page, "3-quick-open");
  await page.keyboard.press("Enter");
  await expect(editorText(page)).toContainText("def shout(s):");
});

test("search finds text and opens the line", async ({ page }) => {
  await openStudio(page);
  await page.keyboard.press("ControlOrMeta+Shift+F");
  await page.getByRole("textbox", { name: /search|поиск|recherche/i }).fill("upper");
  await expect(page.locator(".sr-hit")).toHaveCount(1);
  await shot(page, "4-search");
  await page.locator(".sr-hit").click();
  await expect(editorText(page)).toContainText("s.upper()");
});

test("git panel lists changes and opens a diff against HEAD", async ({ page }) => {
  await openStudio(page);
  await page.keyboard.press("ControlOrMeta+Shift+G");
  const row = page.locator(".git-row", { hasText: "calc.py" });
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.locator(".crumb-tag")).toBeVisible();
  await expect(page.locator(".monaco-diff-editor").first()).toBeVisible();
  await expect(page.locator(".monaco-diff-editor .line-insert, .monaco-diff-editor .char-insert").first()).toBeVisible(); // diff computed
  await shot(page, "5-git-diff");
});

test("create a file from the explorer context menu", async ({ page }) => {
  await openStudio(page);
  await page.getByRole("treeitem", { name: /src/ }).first().click({ button: "right" });
  await page.getByRole("menuitem", { name: /new file|новый файл|nouveau fichier/i }).click();
  await page.getByRole("dialog").getByRole("textbox").fill("made_in_e2e.py");
  await shot(page, "6-new-file");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("treeitem", { name: /made_in_e2e\.py/ })).toBeVisible();
  expect(readFileSync(join(root(), "src/made_in_e2e.py"), "utf8")).toBe("");
});

test("secret files are not loaded until asked", async ({ page }) => {
  await openStudio(page);
  await page.getByRole("treeitem", { name: /\.env/ }).click();
  await expect(page.locator(".gate-inline")).toBeVisible();
  await expect(page.getByText("super-secret-value")).toHaveCount(0);
  await shot(page, "7-secret");
  await page.locator(".gate-inline").getByRole("button").click();
  await expect(editorText(page)).toContainText("API_KEY=");
});
