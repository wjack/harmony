import { expect, type Page } from "@playwright/test";

/**
 * A new visitor's way in (ARCHITECTURE.md §11.4): the intro's Next starts the demo as
 * Alex and shows "What's on screen"; Close this guide leaves the suggested message in his
 * New chat composer, unsent.
 */
export async function enterDemo(page: Page): Promise<void> {
  await page.getByRole("dialog", { name: "Meet Harmony" }).getByRole("button", { name: "Next" }).click();
  await page.getByRole("dialog", { name: "What's on screen" }).getByRole("button", { name: "Close this guide" }).click();
  await expect(page.getByRole("textbox", { name: "Write your message to Claude" })).toHaveValue(/^Find a lab/);
}
