/**
 * The request view on a phone (LIVE_DEMO_PLAN 3.9), against the seeded world: at
 * 390×844 the sidebar is a rail that opens over the page, the view fits the screen with
 * no sideways scroll, Details opens as a sheet over the transcript, and a long message is
 * clamped with "View more" (3.11). Raj opens Ridgeline's "SEM on argon-stored seals",
 * whose Northlake reply is long on purpose.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { unlock } from "./unlock";

const PHONE = { width: 390, height: 844 };
const SEALS = "SEM on argon-stored seals";

/**
 * Acts as `to`, a member outside the walkthrough's accounts: the account menu lists the
 * story's people first and every other member under "All network members".
 */
async function switchToNetworkMember(page: Page, from: string, to: string): Promise<void> {
  await page.getByRole("button", { name: `Account: ${from}` }).click();
  await page.getByRole("menu").getByRole("menuitem", { name: "All network members" }).click();
  await page.getByRole("menuitemradio").filter({ hasText: to }).click();
  await expect(page.getByRole("button", { name: `Account: ${to}` })).toBeVisible();
}

/** What `overflowX` reads of an element. The e2e project has no DOM types to name it. */
interface Measured {
  scrollWidth: number;
  clientWidth: number;
}

/** How far the page, or the view inside it, scrolls sideways. */
async function overflowX(locator: Locator): Promise<number[]> {
  return locator.evaluateAll((elements: Measured[]) =>
    elements.map((element) => element.scrollWidth - element.clientWidth),
  );
}

test("at 390×844 the request view fits, Details is a sheet, and a long message has View more", async ({ page }) => {
  await unlock(page);
  await page.goto("/");
  await page.getByRole("dialog").getByRole("button", { name: /^Network view/ }).click();
  await expect(page).toHaveURL(/\/network/);
  await switchToNetworkMember(page, "Alex Chen", "Raj Patel");

  await page.goto("/requests");
  await page.getByRole("main").getByRole("link").filter({ hasText: SEALS }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: SEALS })).toBeVisible();

  await page.setViewportSize(PHONE);

  await test.step("the sidebar gives the view the room: the rail, opening over the page", async () => {
    const sidebar = page.getByRole("navigation", { name: "Sidebar" });
    await expect.poll(async () => (await sidebar.boundingBox())?.width).toBe(48);
    await page.getByRole("button", { name: "Open sidebar" }).click();
    await expect(sidebar.getByRole("link").filter({ hasText: SEALS })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Open sidebar" })).toBeVisible();
  });

  await test.step("nothing scrolls sideways", async () => {
    await expect.poll(() => overflowX(page.locator("html, body, main"))).toEqual([0, 0, 0]);
  });

  await test.step("a message past 12 lines is clamped, with View more", async () => {
    const more = page.getByRole("button", { name: "View more" }).first();
    await expect(more).toBeVisible();
    await more.click();
    await expect(page.getByRole("button", { name: "View less" }).first()).toBeVisible();
    await expect.poll(() => overflowX(page.locator("html, body, main"))).toEqual([0, 0, 0]);
  });

  await test.step("Details opens as a sheet over the transcript, inside the screen", async () => {
    await page.getByRole("button", { name: "Details" }).click();
    const sheet = page.getByRole("dialog", { name: "Details" });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByRole("heading", { name: "Properties" })).toBeVisible();
    await expect(sheet.getByRole("button", { name: /^What our representative saw/ })).toBeVisible();
    await expect(page.getByRole("complementary", { name: "Properties" })).toHaveCount(0);
    const box = await sheet.boundingBox();
    expect(box).not.toBeNull();
    expect((box?.x ?? -1) >= 0 && (box?.x ?? 0) + (box?.width ?? Infinity) <= PHONE.width).toBe(true);
    await sheet.getByRole("button", { name: "Close details" }).click();
    await expect(sheet).toBeHidden();
    // It opened from the header's button, not a Radix trigger: focus goes back there.
    await expect(page.getByRole("button", { name: "Details" })).toBeFocused();
  });
});
