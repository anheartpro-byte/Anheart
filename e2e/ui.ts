import { expect, type Locator, type Page } from "@playwright/test";

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
