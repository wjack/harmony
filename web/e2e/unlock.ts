import { expect, type Page } from "@playwright/test";

/** The app's master passcode (its `ACCESS_CODE`); playwright.config.ts refuses to run without it. */
export function passcode(): string {
  const value = process.env.E2E_PASSCODE;
  if (value === undefined || value === "") throw new Error("E2E_PASSCODE is not set");
  return value;
}

/**
 * Passes the master passcode gate (LIVE_DEMO_PLAN 3.2) for `page`'s browser context: it
 * posts `E2E_PASSCODE` to `/api/gate` through `page.request`, which shares the
 * context's cookies, so the gate's cookie comes back to every page the test opens.
 * Typing the passcode into the page itself is the locked spec's to check.
 */
export async function unlock(page: Page): Promise<void> {
  const response = await page.request.post("/api/gate", { data: { passcode: passcode(), next: "/" } });
  expect(response.status(), "the passcode gate refused E2E_PASSCODE").toBe(200);
}
