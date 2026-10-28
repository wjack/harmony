/**
 * The suggested story in a browser (REVISION_UNSTRUCTURED.md §10) on ScriptedModel,
 * against the real API, worker, Postgres and Temporal (scripts/e2e_ui.sh).
 *
 * Alex sends the suggested message and approves the outreach for Northlake and Ridgeline
 * on its card at the end of the chat. The orgs' records answer each counterparty's
 * questions, so Ridgeline's conversation closes with nobody asked. Northlake's quote is
 * over the $1,000 Henderson Frame Lab's representative may commit alone, so it reaches Alex
 * inline; he answers, both conversations close and Claude sums up. Then the network
 * view, what Northlake's representative saw, Vantage's probe waiting on Helen, and
 * Customize → Harmony's sharing.
 *
 * The cassettes are keyed by turn, not by what is typed, so the test follows the
 * story's path: Calder is left out, as the walkthrough says. Every browser context is
 * a new visitor, so the test gets a world of its own. Messaged and Replied are passing
 * states on ScriptedModel (a reply or a consult follows within a turn or two), so the
 * network checks wait on the states that hold until a person acts, and on Closed.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { enterDemo } from "./firstVisit";
import { unlock } from "./unlock";

/** Long enough for several model turns on both sides (representative, consult, sharing check). */
const TURNS = { timeout: 90_000 };

const NORTHLAKE = "Northlake Characterization Core";
const RIDGELINE = "Ridgeline Fabrication";
const CALDER = "Calder Computational";
const VANTAGE = "Vantage Market Intelligence";
/** The request's title as Northlake's representative wrote it (the scripted cassette's). */
const NORTHLAKE_TITLE = "CT and compression quote";
/** The seeded Vantage request's title on Northlake's side (threads.yaml). */
const VANTAGE_TITLE = "Rush CT slot enquiry";

test.describe.configure({ timeout: 6 * 60_000 });

async function switchAccount(page: Page, from: string, to: string): Promise<void> {
  await page.getByRole("button", { name: `Account: ${from}` }).click();
  await page.getByRole("menu").getByText(to, { exact: true }).click();
  await expect(page.getByRole("button", { name: `Account: ${to}` })).toBeVisible();
}

function sidebar(page: Page): Locator {
  return page.getByRole("navigation", { name: "Sidebar" });
}

/** Follows a sidebar link; `name` is a prefix, since a badge can follow it ("Requests 2 need you"). */
async function openFromSidebar(page: Page, name: string): Promise<void> {
  await sidebar(page)
    .getByRole("link", { name: new RegExp(`^${name}`) })
    .first()
    .click();
}

/** The network view is the demo's, opened from the demo bar. */
async function openNetworkView(page: Page): Promise<void> {
  await page.getByRole("banner", { name: "Demo controls" }).getByRole("button", { name: "Network view" }).click();
}

/** Opens a chat from the sidebar's Recents, below the requests that share its scroll area. */
async function openChat(page: Page, title: string): Promise<void> {
  await sidebar(page)
    .getByRole("region", { name: "Recents" })
    .getByRole("link", { name: new RegExp(`^${title}`) })
    .first()
    .click();
}

/** Opens Details beside the request, unless the member left it open last time. */
async function openDetails(page: Page): Promise<Locator> {
  // Exact: once the panel is open, its "Close details" button would match too.
  const toggle = page.getByRole("button", { name: "Details", exact: true });
  await expect(toggle).toBeVisible(TURNS);
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  const details = page.getByRole("complementary", { name: "Properties" });
  await expect(details).toBeVisible();
  return details;
}

/** Answers the open inline question from `org`, in the chat. */
async function answerInline(page: Page, org: string, text: string): Promise<void> {
  const box = page.getByRole("textbox", { name: `Your answer about ${org}` });
  await expect(box).toBeVisible(TURNS);
  await box.fill(text);
  await page.getByRole("region").filter({ has: box }).getByRole("button", { name: "Send answer" }).click();
  await expect(box).toBeHidden(TURNS);
}

/** Claude's texts in the chat, oldest first. */
function claudeTexts(page: Page): Locator {
  return page.locator(".chat-prose");
}

/** How many sentences `text` has. */
function sentences(text: string): number {
  return text.trim().split(/(?<=[.!?])\s+/).filter((part) => part !== "").length;
}

/** The live status card's row for `org`. */
function statusRow(page: Page, org: string): Locator {
  return page
    .getByRole("region", { name: "Harmony Network Request", exact: true })
    .last()
    .getByRole("listitem")
    .filter({ hasText: org });
}

/** `org`'s card on the network canvas. */
function networkNode(page: Page, org: string): Locator {
  return page.locator(".react-flow__node").filter({ hasText: org });
}

/** Whether `org`'s name shows in full on its card: no part of it is clipped. */
async function nameFits(page: Page, org: string): Promise<boolean> {
  const name = networkNode(page, org).locator("[data-org-name]");
  const [text, box] = await Promise.all([name.boundingBox(), name.locator("..").boundingBox()]);
  if (text === null || box === null) return false;
  return text.x + text.width <= box.x + box.width + 0.5 && text.y + text.height <= box.y + box.height + 0.5;
}

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Whether `a` and `b` overlap. */
function overlaps(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

async function boxesOf(locator: Locator): Promise<Box[]> {
  const boxes = await Promise.all((await locator.all()).map((each) => each.boundingBox()));
  return boxes.filter((box) => box !== null);
}

/** How many thread lines on the canvas are drawn over a card; each should land on open canvas. */
async function edgeLabelsOverCards(page: Page): Promise<number> {
  const [labels, cards] = await Promise.all([
    boxesOf(page.locator("[data-edge-label]")),
    boxesOf(page.locator(".react-flow__node")),
  ]);
  return labels.filter((label) => cards.some((card) => overlaps(label, card))).length;
}

/** What `clippedValues` reads of an element. The e2e project has no DOM types to name it. */
interface Measured {
  scrollWidth: number;
  clientWidth: number;
  textContent: string | null;
}

/** What `cutShortText` reads of an element. */
interface MeasuredHeight {
  scrollHeight: number;
  clientHeight: number;
  textContent: string | null;
}

/** The decision card's question and draft where they're cut short: taller than their box. */
async function cutShortText(card: Locator): Promise<string[]> {
  return card
    .locator("h2, [aria-busy]")
    .evaluateAll((texts: MeasuredHeight[]) =>
      texts.filter((text) => text.scrollHeight > text.clientHeight + 1).map((text) => text.textContent ?? ""),
    );
}

/** The Details panel's values that are cut short: content wider than the box showing it. */
async function clippedValues(details: Locator): Promise<string[]> {
  return details
    .locator("dd, dd *")
    .evaluateAll((values: Measured[]) =>
      values.filter((value) => value.scrollWidth > value.clientWidth + 1).map((value) => value.textContent ?? ""),
    );
}

// A test of its own: each visitor gets a fresh world, so this one has no request in flight.
test("before any request is in flight, the network view's note sits clear of every card", async ({ page }) => {
  await unlock(page);
  await page.goto("/");
  await enterDemo(page);
  await page.getByRole("banner", { name: "Demo controls" }).getByRole("button", { name: "Network view" }).click();
  await expect(page).toHaveURL(/\/network/);
  const note = page.locator("[data-no-request]");
  await expect(note).toContainText(/No requests yet|Nothing in flight/);
  await expect(page.locator(".react-flow__node").first()).toBeVisible();
  const [box] = await boxesOf(note);
  const cards = await boxesOf(page.locator(".react-flow__node"));
  expect(box).toBeDefined();
  expect(cards.filter((card) => box !== undefined && overlaps(box, card))).toEqual([]);
});

test("the suggested story, from Alex's message to both conversations closed", async ({ page }) => {
  await unlock(page);
  await test.step("Alex sends the suggested message", async () => {
    await page.goto("/");
    await enterDemo(page);
    await page.getByRole("button", { name: "Send message" }).click();
    await expect(page).toHaveURL(/\/chat\//);
  });

  await test.step("at 900px the chat narrows with the window, so nothing runs off its right edge", async () => {
    await page.setViewportSize({ width: 900, height: 800 });
    await expect(page.getByRole("button", { name: "Share", exact: true })).toBeInViewport({ ratio: 1 });
    await expect(page.getByRole("textbox", { name: "Write your message to Claude" })).toBeInViewport({ ratio: 1 });
    await expect(page.getByRole("button", { name: "Use voice mode" })).toBeInViewport({ ratio: 1 });
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  await test.step("the outreach card reads in full at the end of the chat, Calder unticked, and Alex approves it there", async () => {
    const approve = page.getByRole("button", { name: "Approve and send" });
    const card = page.getByRole("region", { name: /^Harmony Network Request / }).filter({ has: approve });
    await expect(card).toBeVisible(TURNS);
    await expect(card.getByRole("textbox", { name: "Brief" })).toHaveValue(/Henderson Frame Lab/);
    for (const org of [NORTHLAKE, RIDGELINE, CALDER]) await expect(card).toContainText(org);
    await expect(card.getByRole("listitem").filter({ hasText: CALDER })).toContainText("Not a fit");
    await expect(card.getByRole("checkbox", { name: CALDER })).not.toBeChecked();
    await expect(card).toContainText(`Starts a conversation with ${NORTHLAKE} and ${RIDGELINE}`);
    // Everything shows in full: nothing is cut behind "View more".
    await expect(page.getByRole("button", { name: "View more" })).toHaveCount(0);

    const before = await claudeTexts(page).count();
    await card.getByRole("button", { name: "Approve and send" }).click();
    // The status card lists only where it went: Calder, left out, isn't on it.
    await expect(statusRow(page, NORTHLAKE)).toBeVisible(TURNS);
    await expect(statusRow(page, RIDGELINE)).toBeVisible();
    await expect(statusRow(page, CALDER)).toHaveCount(0);
    await expect(page.getByRole("region", { name: /^Harmony Network Request / }).getByText("Sent", { exact: true })).toBeVisible();

    // Claude says one sentence: it's out, and where progress shows.
    await expect.poll(async () => (await claudeTexts(page).count()) - before, TURNS).toBe(1);
    await expect.poll(async () => sentences(await claudeTexts(page).last().innerText()), TURNS).toBe(1);
  });

  await test.step("Henderson Frame Lab's records answer Ridgeline, and it closes with nobody asked", async () => {
    await expect(statusRow(page, RIDGELINE)).toContainText("Closed", TURNS);
    await expect(statusRow(page, RIDGELINE)).toContainText("$325");
    await expect(page.getByRole("textbox", { name: `Your answer about ${RIDGELINE}` })).toHaveCount(0);
  });

  await test.step("Northlake's quote is over the $1,000 line, so its review reaches Alex inline", async () => {
    await expect(statusRow(page, NORTHLAKE)).toContainText("Needs your answer", TURNS);
    await expect(page.getByRole("textbox", { name: `Your answer about ${NORTHLAKE}` })).toBeVisible();
    await expect(page.getByText(/Which should I book\?/).first()).toBeVisible();
  });

  await test.step("the network view rings the suggested recipients and shows who waits on a person", async () => {
    await openNetworkView(page);
    for (const org of [NORTHLAKE, RIDGELINE, CALDER]) {
      await expect(networkNode(page, org).locator("[data-suggested]")).toHaveCount(1);
    }
    await expect(networkNode(page, NORTHLAKE)).toContainText("Needs human", TURNS);
    await expect(networkNode(page, RIDGELINE)).toContainText("Closed");
    await expect(networkNode(page, CALDER)).toContainText("Screened");
    expect(await nameFits(page, NORTHLAKE)).toBe(true);
    await expect(page.locator("[data-edge-label]").first()).toBeVisible();
    expect(await edgeLabelsOverCards(page)).toBe(0);
  });

  await test.step("on a 1366×768 laptop the account menu opens below the demo bar, Lock Harmony in view", async () => {
    await page.setViewportSize({ width: 1366, height: 768 });
    await page.getByRole("button", { name: "Account: Alex Chen" }).click();
    const menu = page.getByRole("menu");
    await expect(menu.getByRole("menuitem", { name: "Lock Harmony" })).toBeInViewport({ ratio: 1 });
    const [bar, opened] = await Promise.all([
      page.locator('header[aria-label="Demo controls"]').boundingBox(),
      menu.boundingBox(),
    ]);
    expect(opened?.y ?? 0).toBeGreaterThanOrEqual((bar?.y ?? 0) + (bar?.height ?? Infinity));
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  await test.step("back in the chat, Alex answers the review and both conversations close", async () => {
    await openChat(page, "Find a lab that can CT-scan");
    await answerInline(page, NORTHLAKE, "Yes, take priority.");

    await expect(statusRow(page, NORTHLAKE)).toContainText("Closed", TURNS);
    await expect(statusRow(page, RIDGELINE)).toContainText("Closed");
    await expect(page.getByText(/\$1,759/).first()).toBeVisible(TURNS);
    await expect(page.getByRole("button", { name: "View more" })).toHaveCount(0);
  });

  await test.step("the network view shows both conversations Closed and Calder Screened, at a readable size", async () => {
    await openNetworkView(page);
    await expect(networkNode(page, NORTHLAKE)).toContainText("Closed");
    await expect(networkNode(page, RIDGELINE)).toContainText("Closed");
    await expect(networkNode(page, CALDER)).toContainText("Screened");
    expect(await edgeLabelsOverCards(page)).toBe(0);
    // The pill is 22px tall at zoom 1; below ~0.8 its 12px label stops being readable.
    const pill = networkNode(page, NORTHLAKE).getByText("Closed", { exact: true });
    const box = await pill.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(18);

    // At 1024px, opening a card's panel keeps that card whole and readable.
    await page.setViewportSize({ width: 1024, height: 768 });
    await networkNode(page, NORTHLAKE).click();
    const panel = page.getByRole("complementary", { name: `${NORTHLAKE} details` });
    await expect(panel).toBeVisible();
    await expect.poll(async () => (await pill.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(18);
    await expect
      .poll(async () => {
        const card = await networkNode(page, NORTHLAKE).boundingBox();
        const aside = await panel.boundingBox();
        return card !== null && aside !== null && card.x + card.width <= aside.x;
      })
      .toBe(true);
    await panel.getByRole("button", { name: "Close panel" }).click();
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  await test.step("Northlake's representative saw the released answer, never Raj's note", async () => {
    await switchAccount(page, "Alex Chen", "Dr. Helen Ward");
    // Settled, it's on her Requests page.
    await openFromSidebar(page, "Requests");
    await page.getByRole("main").getByRole("link", { name: new RegExp(`^${NORTHLAKE_TITLE}`) }).first().click();
    const details = await openDetails(page);
    await details.getByRole("button", { name: /^What our representative saw/ }).click();
    const saw = page.getByRole("dialog", { name: "What our representative saw" });
    await expect(saw.getByRole("region", { name: "Released answers" })).toContainText("Wed Nov 4");
    await expect(saw).not.toContainText("Raj's note");
    await expect(saw).not.toContainText("Voltaic");
    await saw.getByRole("button", { name: "Close" }).click();
  });

  await test.step("as Helen, Vantage's probe waits on her, and its decision reads in full", async () => {
    // Acting as Helen started Vantage's request; its second answer names a client's booking.
    const needsYou = sidebar(page).getByRole("region", { name: "Needs you" });
    const probe = needsYou.getByRole("link").filter({ hasText: VANTAGE_TITLE }).first();
    await expect(probe).toBeVisible(TURNS);
    await probe.click();

    const card = page.getByRole("region", { name: "Needs your decision" });
    await expect(card).toContainText(`Claude needs your answer before replying to ${VANTAGE}`, TURNS);
    await expect(card.getByRole("heading", { level: 2 })).toBeVisible();
    // Details beside the transcript at the default laptop width, every value in full.
    const details = await openDetails(page);
    await expect(details).toContainText("Dr. Helen Ward");
    await expect(details).toContainText(VANTAGE);
    expect(await clippedValues(details)).toEqual([]);
    await expect(page.getByText(/other clients and their bookings/).first()).toBeVisible();
    const lookedAt = page.getByRole("region", { name: "What Claude looked at" });
    await expect(lookedAt).toContainText("Client: Voltaic Motors");

    // The decision reads in full at the end of the thread: the whole question and draft.
    expect(await cutShortText(card)).toEqual([]);
    await expect(page.getByRole("button", { name: "View more" })).toHaveCount(0);
  });

  await test.step("Customize → Harmony has only the spec's sections, and sharing edits persist", async () => {
    await openFromSidebar(page, "Customize");
    await expect(page).toHaveURL(/\/customize\/harmony/);
    const settings = page.getByRole("navigation", { name: "Harmony settings" });
    await expect(settings.getByRole("listitem")).toHaveText([
      /^Public profile/,
      /^Instructions/,
      /^Sharing/,
      /^Autonomy/,
      /^Team and routing/,
    ]);

    await settings.getByRole("button", { name: /^Sharing/ }).click();
    const everyone = page.getByRole("textbox", { name: "Shared with everyone" });
    const addition = " Open CT slots on weekdays.";
    const description = `${await everyone.inputValue()}${addition}`;
    await everyone.fill(description);

    await page.getByRole("button", { name: "Add organization" }).click();
    await page.getByRole("menuitem", { name: "Henderson Frame Lab" }).click();
    const mode = page.getByRole("button", { name: /^Mode for Henderson Frame Lab/ });
    const henderson = page.getByRole("listitem").filter({ has: mode });
    await mode.click();
    await page.getByRole("menuitemradio", { name: /^Skip/ }).click();
    const extra = "Priority compression blocks as the public calendar shows them.";
    await henderson.getByRole("textbox", { name: "Also share with them" }).fill(extra);
    await page.getByRole("button", { name: "Save changes" }).click();
    // The org's fields and the trusted organization are separate writes; the toast
    // follows the last of them.
    await expect(page.getByText(/^Settings saved/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Cancel" })).toBeHidden();

    await page.reload();
    await settings.getByRole("button", { name: /^Sharing/ }).click();
    await expect(page.getByRole("textbox", { name: "Shared with everyone" })).toHaveValue(description);
    await expect(mode).toHaveAccessibleName(/: Skip$/);
    await expect(henderson.getByRole("textbox", { name: "Also share with them" })).toHaveValue(extra);
  });
});
