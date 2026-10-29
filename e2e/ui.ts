import { expect, type Locator, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

/** What the scenarios read the dashboard's pages with. */

/** A card of the dashboard, by its title. */
export function card(page: Page, title: string | RegExp) {
  return page
    .locator('[data-slot="card"]')
    .filter({ has: page.locator('[data-slot="card-title"]', { hasText: title }) });
}

/**
 * Picks an option of a select: opened by its trigger, chosen with the
 * keyboard, so that it works wherever the list opens (it can open past the
 * bottom of the window, where a click never lands).
 */
export async function choose(page: Page, trigger: Locator, option: string | RegExp): Promise<void> {
  await trigger.click();
  await page.getByRole("option", { name: option }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("listbox")).toBeHidden();
}

/** `text` as a literal inside a regular expression. */
export const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export type Lang = "fr" | "en";
const catalogs: Record<Lang, Record<string, unknown>> = {
  fr: JSON.parse(readFileSync(path.join(__dirname, "..", "messages", "fr.json"), "utf8")),
  en: JSON.parse(readFileSync(path.join(__dirname, "..", "messages", "en.json"), "utf8")),
};

/** The site's own sentence for `key` (messages/fr.json, messages/en.json), its `{placeholders}` filled. */
export function say(lang: Lang, key: string, values: Record<string, string | number> = {}): string {
  const text = key.split(".").reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], catalogs[lang]);
  if (typeof text !== "string") throw new Error(`message absent : ${key}`);
  return text.replace(/\{(\w+)\}/g, (_, name: string) => String(values[name]));
}
