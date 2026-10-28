/**
 * Window sizing (FEEDBACK_R2_PLAN §2.7, decision 16) on desktop windows from 880 to
 * 1280 px wide, against a fresh world on ScriptedModel: the sidebar is the rail only
 * below 900 px; the chat's and the request view's composers keep a 16 px gutter;
 * scroll-to-latest sits inside its fade; Reset stays in the demo bar; the Details sheet
 * starts below the demo bar; the request's status pill isn't cut at 1000 px; Customize
 * leaves its pane at least 640 px at 1075 px; and no page scrolls sideways.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import { enterDemo } from "./firstVisit";
import { unlock } from "./unlock";

const WIDTHS = [880, 900, 1000, 1075, 1280] as const;
const HEIGHT = 860;
const GUTTER = 16;
const RAIL = 48;
const SIDEBAR = 272;
const SEALS = "SEM on argon-stored seals";

test.describe.configure({ timeout: 3 * 60_000 });

async function atWidth(page: Page, width: number, height = HEIGHT): Promise<void> {
  await page.setViewportSize({ width, height });
}

function sidebar(page: Page): Locator {
  return page.getByRole("navigation", { name: "Sidebar" });
}

function demoBar(page: Page): Locator {
  return page.locator('header[aria-label="Demo controls"]');
}

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

/** What the measuring callbacks read of an element. The e2e project has no DOM types to name it. */
interface Measured {
  scrollWidth: number;
  clientWidth: number;
}

/** How far the page, or the view inside it, scrolls sideways. */
async function overflowX(page: Page): Promise<number[]> {
  return page
    .locator("html, body, main")
    .evaluateAll((elements: Measured[]) => elements.map((element) => element.scrollWidth - element.clientWidth));
}

async function boxOf(locator: Locator): Promise<{ x: number; y: number; width: number; height: number }> {
  const box = await locator.boundingBox();
  if (box === null) throw new Error("not laid out");
  return box;
}

/** The narrower of the gaps between the page's composer and the page's sides. */
async function composerGutter(page: Page): Promise<number> {
  // The composer is the last composer-shaped box on the page: cards above it share the style.
  const [main, composer] = await Promise.all([
    boxOf(page.getByRole("main")),
    boxOf(page.locator("main .rounded-composer").last()),
  ]);
  return Math.min(composer.x - main.x, main.x + main.width - (composer.x + composer.width));
}

/** What `fadeAround` reads of an element. */
interface Faded {
  parentElement: Faded | null;
  children: ArrayLike<Faded>;
  getBoundingClientRect(): { top: number; bottom: number; left: number; right: number };
}
declare const getComputedStyle: (element: Faded) => { backgroundImage: string };

/**
 * The scroll button's box and the nearest gradient around it (itself, an ancestor or a
 * sibling of one): the fade it should sit inside.
 */
async function buttonAndFade(button: Locator) {
  return button.evaluate((element: Faded) => {
    const rect = (node: Faded) => {
      const { top, bottom, left, right } = node.getBoundingClientRect();
      return { top, bottom, left, right };
    };
    for (let node: Faded | null = element; node !== null; node = node.parentElement) {
      for (const candidate of [node, ...Array.from(node.children)]) {
        if (getComputedStyle(candidate).backgroundImage.includes("gradient")) {
          return { button: rect(element), fade: rect(candidate) };
        }
      }
    }
    return { button: rect(element), fade: null };
  });
}

/** Elements under `locator` whose text is cut: wider than the box showing it. */
async function cutShort(locator: Locator): Promise<number> {
  const self = await locator.evaluateAll((elements: Measured[]) =>
    elements.filter((element) => element.scrollWidth > element.clientWidth + 1),
  );
  const inner = await locator
    .locator("*")
    .evaluateAll((elements: Measured[]) => elements.filter((element) => element.scrollWidth > element.clientWidth + 1));
  return self.length + inner.length;
}

/** At each width: the rail only below 900 px, Reset in view, nothing sideways. */
async function checkFrame(page: Page, width: number): Promise<void> {
  await expect.poll(async () => (await sidebar(page).boundingBox())?.width).toBe(width < 900 ? RAIL : SIDEBAR);
  await expect(demoBar(page).getByRole("button", { name: "Reset" })).toBeInViewport({ ratio: 1 });
  await expect.poll(() => overflowX(page)).toEqual([0, 0, 0]);
}

test("the chat keeps its gutter, Reset its place and the sidebar its width, from 880 to 1280 px", async ({
  page,
}) => {
  await unlock(page);
  await page.goto("/");
  await enterDemo(page);
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page).toHaveURL(/\/chat\//);

  for (const width of WIDTHS) {
    await test.step(`at ${width} px`, async () => {
      await atWidth(page, width);
      await checkFrame(page, width);
      expect(await composerGutter(page)).toBeGreaterThanOrEqual(GUTTER);
    });
  }
});

/** Starts the demo on the network view, as Alex. */
async function startOnNetwork(page: Page): Promise<void> {
  await unlock(page);
  await page.goto("/");
  await enterDemo(page);
  await page.getByRole("banner", { name: "Demo controls" }).getByRole("button", { name: "Network view" }).click();
  await expect(page).toHaveURL(/\/network/);
}

test("the request view keeps its gutter and pill, and Details opens below the demo bar", async ({ page }) => {
  await startOnNetwork(page);
  await switchToNetworkMember(page, "Alex Chen", "Raj Patel");

  await page.goto("/requests");
  await page.getByRole("main").getByRole("link").filter({ hasText: SEALS }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: SEALS })).toBeVisible();

  for (const width of WIDTHS) {
    await test.step(`the request view at ${width} px`, async () => {
      await atWidth(page, width);
      await checkFrame(page, width);
      expect(await composerGutter(page)).toBeGreaterThanOrEqual(GUTTER);
    });
  }

  await test.step("at 1000 px the status pill shows whole, and Details opens as a sheet below the demo bar", async () => {
    await atWidth(page, 1000);
    const pill = page.getByRole("main").locator("header [title]").first();
    await expect(pill).toBeVisible();
    expect(await cutShort(pill)).toBe(0);

    await page.getByRole("button", { name: "Details", exact: true }).click();
    const sheet = page.getByRole("dialog", { name: "Details" });
    await expect(sheet).toBeVisible();
    const [bar, box] = await Promise.all([boxOf(demoBar(page)), boxOf(sheet)]);
    expect(box.y).toBeGreaterThanOrEqual(bar.y + bar.height);
    await sheet.getByRole("button", { name: "Close details" }).click();
    await expect(sheet).toBeHidden();
  });

  await test.step("scrolled up, the scroll-to-latest button sits inside its fade", async () => {
    // Short enough that Northlake's long reply scrolls.
    await atWidth(page, 1000, 600);
    const main = await boxOf(page.getByRole("main"));
    await page.mouse.move(main.x + main.width / 2, main.y + 200);
    await page.mouse.wheel(0, -10_000);
    const button = page.getByRole("button", { name: "Scroll to the latest message" });
    await expect(button).toBeVisible();
    const { button: at, fade } = await buttonAndFade(button);
    expect(fade).not.toBeNull();
    if (fade === null) return;
    expect(at.top).toBeGreaterThanOrEqual(fade.top);
    expect(at.bottom).toBeLessThanOrEqual(fade.bottom);
  });
});

test("Customize and the network view fit every width", async ({ page }) => {
  await startOnNetwork(page);

  await test.step("Customize: its own column only from 1280 px, and a pane of at least 640 px at 1075", async () => {
    await page.goto("/customize/harmony?section=profile");
    const pane = page.locator('[aria-label="Public profile"]');
    const column = page.getByRole("navigation", { name: "Customize" });
    const list = page.getByRole("navigation", { name: "Harmony settings" });
    for (const width of WIDTHS) {
      await atWidth(page, width);
      await expect(pane).toBeVisible();
      await expect.poll(() => overflowX(page)).toEqual([0, 0, 0]);
      if (width >= 1280) {
        await expect(column).toBeVisible();
      } else {
        await expect(column).toHaveCount(0);
        // The list carries the way back instead.
        await expect(page.getByRole("button", { name: "Back" })).toBeVisible();
      }
      await expect(list).toBeVisible();
    }
    await atWidth(page, 1075);
    // The 1280-only column leaves on the next render, not with the resize itself.
    await expect(column).toHaveCount(0);
    await expect.poll(async () => (await boxOf(pane)).width).toBeGreaterThanOrEqual(640);
  });

  await test.step("the network view: the rail only below 900 px, and its header fits", async () => {
    await page.goto("/network");
    await expect(page.locator(".react-flow__node").first()).toBeVisible();
    for (const width of WIDTHS) {
      await atWidth(page, width);
      await checkFrame(page, width);
      await expect(page.getByRole("radiogroup", { name: "World" })).toBeInViewport({ ratio: 1 });
    }
  });
});
