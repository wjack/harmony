/**
 * The request view against the seeded world on ScriptedModel (FEEDBACK_R2_PLAN §2.4,
 * EDIT.md §9, REOPEN.md §10.6), at desktop sizes.
 *
 * Raj opens Ridgeline's "SEM on argon-stored seals" at 1000×860. Its Northlake reply is
 * long on purpose: it shows in full, Details opens as a sheet below the demo bar,
 * nothing scrolls sideways, and the seeded thread reads Closed with no Reopen.
 *
 * Then the Vantage request, which Helen's first switch into Northlake starts: she uses
 * Edit on the consult that waits for her, approves, and reopens the conversation once
 * Vantage closes it. She reopens in Ask, so Edit is checked while Claude's reopened
 * message waits for her: the cassette's reopened run closes again moments after that
 * message is sent. Vantage's side is checked by switching to Jordan in the same
 * session, since another browser context would get a world of its own (LIVE_DEMO_PLAN
 * 3.3). The texts are §1.9's, which the cassettes are written for.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { unlock } from "./unlock";

/** Long enough for several model turns on both sides (representative, consult, sharing check). */
const TURNS = { timeout: 90_000 };
const LAPTOP = { width: 1000, height: 860 };

const SEALS = "SEM on argon-stored seals";
const VANTAGE_REQUEST = "Rush SEM slot enquiry";
const VANTAGE = "Vantage Market Intelligence";
const NORTHLAKE = "Northlake Characterization Core";
const EDIT_TEXT = "Don't say who books the afternoons. Just say they aren't available.";
const GUIDANCE_V1 = "Don't say which client books any of Northlake's slots. Say only whether a slot is available.";
const REOPEN_TEXT = "Offer them our general capabilities sheet instead.";

test.describe.configure({ timeout: 6 * 60_000 });

/** Starts the demo on the network view, as Alex. */
async function startOnNetwork(page: Page): Promise<void> {
  await unlock(page);
  await page.goto("/");
  await page.getByRole("dialog").getByRole("button", { name: "Try the suggested request", exact: true }).click();
  await page.getByRole("banner", { name: "Demo controls" }).getByRole("button", { name: "Network view" }).click();
  await expect(page).toHaveURL(/\/network/);
}

/** Acts as `to`, one of the walkthrough's accounts, listed at the account menu's top level. */
async function switchAccount(page: Page, from: string, to: string): Promise<void> {
  await page.getByRole("button", { name: `Account: ${from}` }).click();
  await page.getByRole("menu").getByText(to, { exact: true }).click();
  await expect(page.getByRole("button", { name: `Account: ${to}` })).toBeVisible();
}

/** Acts as `to`, a member outside the walkthrough's accounts, under "All network members". */
async function switchToNetworkMember(page: Page, from: string, to: string): Promise<void> {
  await page.getByRole("button", { name: `Account: ${from}` }).click();
  await page.getByRole("menu").getByRole("menuitem", { name: "All network members" }).click();
  await page.getByRole("menuitemradio").filter({ hasText: to }).click();
  await expect(page.getByRole("button", { name: `Account: ${to}` })).toBeVisible();
}

function card(page: Page): Locator {
  return page.getByRole("region", { name: "Needs your decision" });
}

/** The view's box, for a comment to the team or an Edit to our Claude. */
function box(page: Page): Locator {
  return page.getByRole("region", { name: "Comment or Edit" });
}

function conversation(page: Page): Locator {
  return page.getByRole("list", { name: "Conversation" });
}

/** The latest run's Closed row. */
function closedRow(page: Page): Locator {
  return page.getByRole("region", { name: /^Closed / }).last();
}

/** What the measuring callbacks read of an element. The e2e project has no DOM types to name it. */
interface Measured {
  scrollWidth: number;
  clientWidth: number;
  scrollHeight: number;
  clientHeight: number;
}

/** How far the page, or the view inside it, scrolls sideways. */
async function overflowX(page: Page): Promise<number[]> {
  return page
    .locator("html, body, main")
    .evaluateAll((elements: Measured[]) => elements.map((element) => element.scrollWidth - element.clientWidth));
}

/** How much of an element's text is cut off below its box. */
async function cutShort(locator: Locator): Promise<number> {
  return locator.evaluate((element: Measured) => element.scrollHeight - element.clientHeight);
}

async function boxOf(locator: Locator): Promise<{ x: number; y: number; width: number; height: number }> {
  const found = await locator.boundingBox();
  if (found === null) throw new Error("not laid out");
  return found;
}

test("at 1000×860 a seeded request fits: Details below the demo bar, a long reply in full, Closed with no Reopen", async ({
  page,
}) => {
  await startOnNetwork(page);
  await switchToNetworkMember(page, "Alex Chen", "Raj Patel");
  await page.setViewportSize(LAPTOP);
  await page.goto("/requests");
  await page.getByRole("main").getByRole("link").filter({ hasText: SEALS }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: SEALS })).toBeVisible();

  await test.step("Northlake's long reply shows in full, with no View more", async () => {
    const reply = conversation(page).getByRole("article").filter({ hasText: "Yes, we can image both." });
    await expect(reply).toContainText("add air-free transfer at $150 for the batch.");
    expect(await cutShort(reply.locator("p").first())).toBeLessThanOrEqual(0);
    await expect(page.getByRole("button", { name: "View more" })).toHaveCount(0);
  });

  await test.step("the seeded thread reads Closed, with no Reopen", async () => {
    await expect(closedRow(page)).toContainText(/^Closed · /);
    await expect(closedRow(page).getByRole("button", { name: "Reopen" })).toHaveCount(0);
  });

  await test.step("nothing scrolls sideways", async () => {
    await expect.poll(() => overflowX(page)).toEqual([0, 0, 0]);
  });

  await test.step("Details opens as a sheet that starts below the demo bar", async () => {
    await page.getByRole("button", { name: "Details", exact: true }).click();
    const sheet = page.getByRole("dialog", { name: "Details" });
    await expect(sheet).toBeVisible();
    await expect(page.getByRole("complementary", { name: "Properties" })).toHaveCount(0);
    const [bar, opened] = await Promise.all([boxOf(page.locator('header[aria-label="Demo controls"]')), boxOf(sheet)]);
    expect(opened.y).toBeGreaterThanOrEqual(bar.y + bar.height);
    expect(opened.x + opened.width).toBeLessThanOrEqual(LAPTOP.width);
    await sheet.getByRole("button", { name: "Close details" }).click();
    await expect(sheet).toBeHidden();
    // It opened from the header's button, not a Radix trigger: focus goes back there.
    await expect(page.getByRole("button", { name: "Details", exact: true })).toBeFocused();
  });
});

test("Helen tells Claude what to do differently, approves, and reopens; Vantage sees only that Northlake reopened", async ({
  page,
}) => {
  await startOnNetwork(page);
  let thread = "";

  await test.step("Helen's first switch starts the Vantage request, whose second consult waits for her", async () => {
    await switchAccount(page, "Alex Chen", "Dr. Helen Ward");
    const link = page
      .getByRole("navigation", { name: "Sidebar" })
      .getByRole("region", { name: "Needs you" })
      .getByRole("link")
      .filter({ hasText: VANTAGE_REQUEST })
      .first();
    await expect(link).toBeVisible(TURNS);
    await link.click();
    await expect(card(page)).toContainText(`Claude needs your answer before replying to ${VANTAGE}`, TURNS);
    thread = page.url();
  });

  await test.step("she uses Edit, and Claude rewrites its draft and follows her instruction", async () => {
    await box(page).getByRole("radio", { name: "Edit" }).click();
    await box(page).getByRole("textbox", { name: "Tell Claude" }).fill(EDIT_TEXT);
    await box(page).getByRole("button", { name: "Tell Claude" }).click();
    await expect(card(page).getByText("Claude is rewriting…")).toBeVisible();

    await expect(card(page).getByText("· rewritten by Claude")).toBeVisible(TURNS);
    await expect(card(page).getByText("2 of 2")).toBeVisible();
    await expect(card(page).getByRole("textbox", { name: "Draft reply" })).toHaveValue(/aren't available/);
    // The box goes back to Comment after each Edit.
    await expect(box(page).getByRole("radio", { name: "Comment" })).toHaveAttribute("aria-checked", "true");

    const row = conversation(page).getByRole("article", { name: "Helen told Claude what to do differently" });
    await expect(row).toContainText(EDIT_TEXT);
    await row.getByRole("button", { name: "See guidance" }).click();
    const guidance = page.getByRole("region", { name: "Guidance Claude follows" });
    await expect(guidance).toContainText(GUIDANCE_V1);
    await expect(guidance).toContainText("Version 1");
  });

  await test.step("Jordan, at Vantage, sees none of it", async () => {
    await switchToNetworkMember(page, "Dr. Helen Ward", "Jordan Pike");
    await page.goto(thread);
    await expect(conversation(page)).toBeVisible();
    await expect(page.getByText(EDIT_TEXT)).toHaveCount(0);
    await expect(page.getByText(GUIDANCE_V1)).toHaveCount(0);
    await expect(page.getByRole("article", { name: /told Claude what to do differently$/ })).toHaveCount(0);
  });

  await test.step("back as Helen, she approves; Vantage closes the conversation, and Edit turns off", async () => {
    await switchAccount(page, "Jordan Pike", "Dr. Helen Ward");
    await page.goto(thread);
    const approve = card(page).getByRole("button", { name: "Approve and send" });
    await approve.click();
    await expect(approve).toBeHidden(TURNS);
    await expect(closedRow(page)).toContainText(new RegExp(`^Closed · .+ · ${VANTAGE} closed it`), TURNS);
    await expect(box(page).getByRole("radio", { name: "Edit" })).toHaveAttribute("aria-disabled", "true");
  });

  await test.step("she sets Northlake to Ask and reopens it with what Claude should pick up", async () => {
    // In Ask, Claude's reopened message waits for her, so the Edit check below sees the
    // conversation open (the reopened run closes again a second or two after it's sent).
    await page.goto("/customize/harmony?section=autonomy");
    await page.getByRole("radiogroup", { name: "Default mode" }).getByText("Ask", { exact: true }).click();
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText(/^Settings saved/)).toBeVisible();

    await page.goto(thread);
    await closedRow(page).getByRole("button", { name: "Reopen" }).click();
    const prompt = page.getByRole("region", { name: `Reopen with ${VANTAGE}` });
    await prompt.getByRole("textbox", { name: "What should Claude pick up?" }).fill(REOPEN_TEXT);
    await prompt.getByRole("button", { name: "Reopen" }).click();
    await expect(
      conversation(page).getByRole("article", { name: /^Helen (is reopening|reopened) the conversation$/ }),
    ).toBeVisible(TURNS);
  });

  await test.step("while Claude's message waits for her, Edit works again; she approves it", async () => {
    const approve = card(page).getByRole("button", { name: "Approve and send" });
    await expect(approve).toBeVisible(TURNS);
    const edit = box(page).getByRole("radio", { name: "Edit" });
    await expect(edit).not.toHaveAttribute("aria-disabled", "true");
    await edit.click();
    await expect(box(page).getByRole("textbox", { name: "Tell Claude" })).toBeVisible();
    await approve.click();
    await expect(approve).toBeHidden(TURNS);
  });

  await test.step("Jordan sees that Northlake reopened it, not what Helen typed, then Northlake's message", async () => {
    await switchToNetworkMember(page, "Dr. Helen Ward", "Jordan Pike");
    await page.goto(thread);
    await expect(
      conversation(page).getByRole("article", { name: `${NORTHLAKE} reopened the conversation` }),
    ).toBeVisible(TURNS);
    await expect(conversation(page)).toContainText("general capabilities sheet", TURNS);
    await expect(page.getByText(REOPEN_TEXT)).toHaveCount(0);
  });
});
