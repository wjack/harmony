/**
 * Switching accounts with two tabs open (FEEDBACK_R2_PLAN §2.6, switching.html), on
 * ScriptedModel against the real API and worker.
 *
 * Alex's chat is open in both tabs when one switches to Helen. That is the first switch
 * into Northlake, which starts the seeded Vantage request before the act-as answers, so
 * the switch's event reaches the tabs before its response does. Both tabs follow to
 * Helen's section in place: no reload, and nothing fetched as Helen for Alex's chat.
 * Then a pasted link to Alex's chat, as Helen, says whose it is, and its switch opens it.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { enterDemo } from "./firstVisit";
import { unlock } from "./unlock";

/** Long enough for the story's first turns on ScriptedModel. */
const TURNS = { timeout: 90_000 };

test.describe.configure({ timeout: 4 * 60_000 });

async function switchAccount(page: Page, from: string, to: string): Promise<void> {
  await page.getByRole("button", { name: `Account: ${from}` }).click();
  await page.getByRole("menu").getByText(to, { exact: true }).click();
}

/** The outreach card waiting on Alex at the end of his chat. */
function outreachCard(page: Page): Locator {
  return page
    .getByRole("region", { name: /^Harmony Network Request / })
    .filter({ has: page.getByRole("button", { name: "Approve and send" }) });
}

/** Marks the page, so a reload (which loses the mark) shows. */
async function mark(page: Page): Promise<void> {
  await page.evaluate(() => {
    (globalThis as unknown as Record<string, unknown>).harmonySwitchMark = true;
  });
}

async function stillMarked(page: Page): Promise<boolean> {
  return page.evaluate(() => (globalThis as unknown as Record<string, unknown>).harmonySwitchMark === true);
}

test("every tab follows a switch in place, and a pasted link to someone's chat says whose it is", async ({
  context,
}) => {
  const a = await context.newPage();
  await unlock(a);

  const chatUrl = await test.step("Alex starts a chat, and it waits on him", async () => {
    await a.goto("/");
    await enterDemo(a);
    await a.getByRole("button", { name: "Send message" }).click();
    await expect(a).toHaveURL(/\/chat\//);
    // The chat is quiet once Claude asks Alex about the outreach, so nothing in it
    // refetches while the switch is under way.
    await expect(outreachCard(a)).toBeVisible(TURNS);
    return a.url();
  });

  const b = await context.newPage();
  await b.goto(chatUrl);
  await expect(outreachCard(b)).toBeVisible(TURNS);

  await test.step("switching to Helen in one tab moves both to her New chat, without a reload or a 404", async () => {
    const notFound: string[] = [];
    for (const page of [a, b]) {
      await mark(page);
      page.on("response", (response) => {
        if (response.status() === 404 && new URL(response.url()).pathname.startsWith("/api/")) {
          notFound.push(response.url());
        }
      });
    }

    await switchAccount(a, "Alex Chen", "Dr. Helen Ward");

    for (const page of [a, b]) {
      await expect(page.getByRole("button", { name: "Account: Dr. Helen Ward" })).toBeVisible(TURNS);
      await expect(page).toHaveURL(/\/$/);
      expect(await stillMarked(page)).toBe(true);
      await expect(page.getByText(/isn't in this demo|This chat is /)).toHaveCount(0);
    }
    await expect(a.getByText("Switched to Dr. Helen Ward")).toHaveCount(1);
    await expect(b.getByText("Switched to Dr. Helen Ward")).toHaveCount(0);
    expect(notFound).toEqual([]);
  });

  await test.step("a pasted link to Alex's chat, as Helen, says whose it is, and its switch opens it", async () => {
    await b.goto(chatUrl);
    await expect(b.getByRole("heading", { name: "This chat is Alex Chen's" })).toBeVisible();
    await expect(b.getByText(/You're acting as Dr\. Helen Ward at Northlake\./)).toBeVisible();

    await mark(a);
    await b.getByRole("button", { name: "Switch to Alex" }).click();

    await expect(b.getByRole("button", { name: "Account: Alex Chen" })).toBeVisible(TURNS);
    expect(b.url()).toBe(chatUrl);
    await expect(outreachCard(b)).toBeVisible(TURNS);
    // The other tab follows too, staying on New chat.
    await expect(a.getByRole("button", { name: "Account: Alex Chen" })).toBeVisible(TURNS);
    await expect(a).toHaveURL(/\/$/);
    expect(await stillMarked(a)).toBe(true);
  });
});
