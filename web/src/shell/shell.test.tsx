// @vitest-environment jsdom
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { queryKeys } from "../api/queryKeys";
import type { InboxItem, SessionView } from "../api/types";
import { clearAllAlerts } from "../demo/alerts";
import { demoStore } from "../demo/store";
import { problem } from "../fixtures";
import { alexInbox, conversations, FIXTURE_NOW, members, sessionAs } from "../fixtures/shell";
import { needsHuman } from "../realtime/testing";
import {
  demoBar,
  FakeServer,
  Gate,
  NEW_WORLD_ID,
  openAccountMenu,
  renderLive,
  renderShell,
  watchFor,
} from "../test/shellHarness";
import { RECONNECT_GRACE_MS } from "./AppLayout";
import { showRail, sidebarStore, toggleSidebar } from "./sidebarStore";
import { toastStore } from "./toasts";

const RECENT = conversations[0]?.title ?? "";
// Helen's inbox (fixtures/shell.ts): item n is escalation …3e<n> in thread …3b<n>.
const ESCALATION = (n: number) => `0b1d7a3e-0000-4000-8000-0000000003e${n}`;
const THREAD = (n: number) => `0b1d7a3e-0000-4000-8000-0000000003b${n}`;

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
  window.localStorage.clear();
  demoStore.setState({
    welcomed: false,
    introOpen: false,
    controlsOpen: false,
    resetConfirmOpen: false,
    introHelp: false,
    prefillSuggestion: false,
    userGuide: false,
    // The tip after the intro's guide is HelpOverlay.test's; here it would be one more status.
    guideTip: false,
    guideTipShown: true,
  });
  sidebarStore.setState({ collapsed: false });
  toastStore.setState({ toasts: [] });
  clearAllAlerts();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("introduces the demo, starts it as Alex and switches accounts over a fresh stream", async () => {
  const server = new FakeServer(null);
  const { dispatcher, sources } = renderShell(server);
  await act(() => dispatcher.start());

  // First visit, no session: the landing, with the intro open over it.
  // (The open modal hides the page from the accessibility tree, so find it by text.)
  expect(screen.getByText("Welcome to Harmony")).toBeTruthy();
  const intro = await screen.findByRole("dialog", { name: "Meet Harmony" });
  expect(sources.opened).toHaveLength(0);

  fireEvent.click(within(intro).getByRole("button", { name: "Next" }));

  // Starting creates the session; the shell comes up as Alex with Recents, under "What's
  // on screen" (walkthrough.test.tsx follows it to the composer).
  const help = await screen.findByRole("dialog", { name: "What's on screen" });
  expect(screen.queryByRole("dialog", { name: "Meet Harmony" })).toBeNull();
  expect(demoStore.getState().welcomed).toBe(true);
  fireEvent.keyDown(help, { key: "Escape" });
  await screen.findByRole("button", { name: "Account: Alex Chen" });
  expect(await screen.findByText(RECENT)).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(sources.opened.map((source) => source.url)).toEqual(["/api/events?after=7&epoch=1"]);
  act(() => {
    sources.latest.accept();
  });

  // Switch to Helen from the account menu: the stream reopens after act-as's cursor,
  // and the frame refetches as Helen (her Requests badge, no Recents).
  const menu = await openAccountMenu(members.alex.name);
  fireEvent.click(within(menu).getByRole("menuitemradio", { name: /Dr\. Helen Ward/ }));

  await screen.findByRole("button", { name: "Account: Dr. Helen Ward" });
  expect(await screen.findByLabelText("2 need you")).toBeTruthy();
  expect(screen.queryByText(RECENT)).toBeNull();
  expect(screen.getByRole("status").textContent).toContain("Switched to Dr. Helen Ward");
  expect(sources.opened.map((source) => source.url)).toEqual([
    "/api/events?after=7&epoch=1",
    "/api/events?after=12&epoch=2",
  ]);
  expect(sources.opened[0]?.closed).toBe(true);

  dispatcher.stop();
});

it("keeps showing the previous account, inert, while a switch is in flight", async () => {
  const { server, dispatcher } = await renderLive(sessionAs(members.alex, 1));
  expect(await screen.findByText(RECENT)).toBeTruthy();
  const notStarted = watchFor("Not started");
  server.sessionGate = new Gate();

  const menu = await openAccountMenu(members.alex.name);
  fireEvent.click(within(menu).getByRole("menuitemradio", { name: /Dr\. Helen Ward/ }));

  // The new snapshot is held. The frame still shows Alex, with his Recents, and can't
  // be used, since the server already acts as Helen.
  await waitFor(() => {
    expect(server.count("GET /api/session")).toBe(2);
  });
  expect(dispatcher.state.status).toBe("resetting");
  expect(demoBar().textContent).toContain("Acting asAlex Chen");
  expect(screen.getByText(RECENT)).toBeTruthy();
  expect(document.querySelector("[inert]")?.getAttribute("aria-busy")).toBe("true");

  server.sessionGate.open();
  await screen.findByRole("button", { name: "Account: Dr. Helen Ward" });
  expect(demoBar().textContent).toContain("Acting asDr. Helen Ward");
  expect(document.querySelector("[inert]")).toBeNull();

  // One snapshot per switch: the old frame never emptied, so it never refetched.
  expect(server.count("GET /api/session")).toBe(2);
  expect(notStarted.seen()).toBe(false);
  notStarted.stop();
  dispatcher.stop();
});

it("turns a needs-human cue for another account into a chip that opens the item as them", async () => {
  const { server, dispatcher, sources } = await renderLive(sessionAs(members.alex, 1));

  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ESCALATION(1)));
  });

  // A demo-bar chip at once, and a toast naming the assignee once the directory loads.
  const chip = screen.getByRole("button", { name: /Northlake needs a human.*Switch to Helen/ });
  const toastRegion = screen.getByRole("status");
  expect(await within(toastRegion).findByText(/Northlake needs a human\./)).toBeTruthy();
  expect(within(toastRegion).getByRole("button", { name: "Switch to Helen" })).toBeTruthy();

  fireEvent.click(chip);

  // Acting as Helen, her inbox resolves the escalation to its thread.
  await screen.findByRole("button", { name: "Account: Dr. Helen Ward" });
  expect(await screen.findByText(`Thread at /requests/${THREAD(1)}`)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Switch to Helen/ })).toBeNull();
  expect(sources.latest.url).toBe("/api/events?after=12&epoch=2");
  expect(server.acting?.slug).toBe(members.helen.slug);

  dispatcher.stop();
});

it("says why a switch failed, keeps the account and the chip, and never rejects", async () => {
  const { server, dispatcher, sources } = await renderLive(sessionAs(members.alex, 1));
  server.actAsFailure = () => problem(409, "world_retired", "World is retired.");
  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ESCALATION(1)));
  });

  fireEvent.click(screen.getByRole("button", { name: /Northlake needs a human.*Switch to Helen/ }));

  expect(
    await within(screen.getByRole("status")).findByText("Couldn't switch to Dr. Helen Ward: World is retired."),
  ).toBeTruthy();
  expect(screen.getByRole("button", { name: "Account: Alex Chen" })).toBeTruthy();
  expect(screen.getByRole("button", { name: /Northlake needs a human.*Switch to Helen/ })).toBeTruthy();
  expect(sources.opened).toHaveLength(1);
  expect(dispatcher.state.status).toBe("open");

  dispatcher.stop();
});

it("points the acting member's own cue at the item in their Requests", async () => {
  const { server, dispatcher, sources } = await renderLive(sessionAs(members.helen, 1));

  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ESCALATION(2)));
  });

  expect(await screen.findByText(/Northlake needs you\./)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /needs a human/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Open Requests" }));
  expect(await screen.findByText(`Thread at /requests/${THREAD(2)}`)).toBeTruthy();
  expect(server.calls).not.toContain("POST /api/session/act-as");

  dispatcher.stop();
});

// Alex's question (fixtures/shell.ts alexInbox) is answered inline in his first chat.
const QUESTION = ESCALATION(4);
const QUESTION_CHAT = alexInbox.needs_you[0]?.conversation_id ?? "";

it("points the acting member's own cue for a question in their chat at that chat", async () => {
  const { server, dispatcher, sources } = await renderLive(sessionAs(members.alex, 1));
  server.alexInbox = alexInbox;

  act(() => {
    sources.latest.send(needsHuman(8, members.alex.slug, QUESTION, "henderson"));
  });

  const toastRegion = screen.getByRole("status");
  expect(await within(toastRegion).findByText("Ridgeline Fabrication's question needs your answer.")).toBeTruthy();
  expect(within(toastRegion).queryByText(/Henderson needs you/)).toBeNull();
  fireEvent.click(within(toastRegion).getByRole("button", { name: "Answer in the chat" }));
  expect(await screen.findByText(`Chat at /chat/${QUESTION_CHAT}`)).toBeTruthy();

  dispatcher.stop();
});

it("opens a question in the assignee's chat there when a chip switches to them", async () => {
  const { server, dispatcher, sources } = await renderLive(sessionAs(members.helen, 1));
  server.alexInbox = alexInbox;

  act(() => {
    sources.latest.send(needsHuman(8, members.alex.slug, QUESTION, "henderson"));
  });
  fireEvent.click(screen.getByRole("button", { name: /Henderson needs a human.*Switch to Alex/ }));

  await screen.findByRole("button", { name: "Account: Alex Chen" });
  expect(await screen.findByText(`Chat at /chat/${QUESTION_CHAT}`)).toBeTruthy();

  dispatcher.stop();
});

it("treats a cue for the same member slug at another org as someone else's", async () => {
  const { dispatcher, sources } = await renderLive(sessionAs(members.helen, 1));

  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ESCALATION(3), "cascade"));
  });

  expect(screen.getByRole("button", { name: /Cascade needs a human.*Switch to helen/ })).toBeTruthy();
  expect(await within(screen.getByRole("status")).findByText(/Cascade needs a human\./)).toBeTruthy();
  expect(screen.queryByText(/needs you\./)).toBeNull();

  dispatcher.stop();
});

it("lands on the section when the page was the previous account's own item", async () => {
  const { dispatcher } = await renderLive(sessionAs(members.helen, 1), { path: `/requests/${THREAD(1)}` });
  expect(screen.getByText(`Thread at /requests/${THREAD(1)}`)).toBeTruthy();

  const menu = await openAccountMenu(members.helen.name);
  fireEvent.click(within(menu).getByRole("menuitemradio", { name: /Alex Chen/ }));

  await screen.findByRole("button", { name: "Account: Alex Chen" });
  expect(await screen.findByText("Requests at /requests")).toBeTruthy();

  dispatcher.stop();
});

it("switches to Alex for the suggested request the way every switch does", async () => {
  const { dispatcher } = await renderLive(sessionAs(members.helen, 1), { path: `/requests/${THREAD(1)}` });

  fireEvent.click(screen.getByRole("button", { name: "Demo controls" }));
  const controls = await screen.findByRole("dialog", { name: "Demo controls" });
  fireEvent.click(within(controls).getByRole("button", { name: "Try the suggested request" }));

  await screen.findByRole("button", { name: "Account: Alex Chen" });
  await waitFor(() => {
    expect(screen.queryByRole("dialog", { name: "Demo controls" })).toBeNull();
  });
  expect(await screen.findByText("Home at /")).toBeTruthy();
  expect(screen.getByRole("status").textContent).toContain("Switched to Alex Chen");

  dispatcher.stop();
});

it("resets into a new world: clears the cues, lands on New chat and rebuilds the cache there", async () => {
  const { server, dispatcher, sources, queryClient } = await renderLive(sessionAs(members.alex, 1), {
    path: "/network",
  });
  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ESCALATION(1)));
  });
  expect(screen.getByRole("button", { name: /Northlake needs a human/ })).toBeTruthy();
  await within(screen.getByRole("status")).findByText(/Northlake needs a human\./);

  fireEvent.click(screen.getByRole("button", { name: "Reset" }));
  const confirm = await screen.findByRole("dialog", { name: "Reset the demo?" });
  fireEvent.click(within(confirm).getByRole("button", { name: "Reset demo" }));

  await waitFor(() => {
    expect(sources.latest.url).toBe("/api/events?after=3&epoch=2");
  });
  expect(server.calls).toContain("POST /api/worlds/reset");
  expect(await screen.findByText("Home at /")).toBeTruthy();
  expect(queryClient.getQueryData<SessionView>(queryKeys.session)?.world_id).toBe(NEW_WORLD_ID);
  expect(screen.queryByRole("button", { name: /needs a human/ })).toBeNull();
  const toasts = screen.getByRole("status").textContent;
  expect(toasts).toContain("Demo reset");
  expect(toasts).not.toContain("needs a human");
  expect(screen.queryByRole("dialog", { name: "Reset the demo?" })).toBeNull();

  dispatcher.stop();
});

it("resets back to the start of the story, as Alex, whoever was acting", async () => {
  const { dispatcher } = await renderLive(sessionAs(members.helen, 1), { path: `/requests/${THREAD(1)}` });

  fireEvent.click(screen.getByRole("button", { name: "Reset" }));
  const confirm = await screen.findByRole("dialog", { name: "Reset the demo?" });
  expect(confirm.textContent).toContain("back at the start of the story");
  fireEvent.click(within(confirm).getByRole("button", { name: "Reset demo" }));

  expect(await screen.findByRole("button", { name: "Account: Alex Chen" })).toBeTruthy();
  expect(await screen.findByText("Home at /")).toBeTruthy();

  dispatcher.stop();
});

it("opens the not-available dialog, naming the feature, from the sidebar and the account menu", async () => {
  const { dispatcher } = await renderLive(sessionAs(members.alex, 1));

  fireEvent.click(screen.getByRole("button", { name: "Projects" }));
  let dialog = await screen.findByRole("dialog", { name: "This isn't part of the Harmony prototype" });
  expect(dialog.textContent).toContain("Projects isn't included.");
  fireEvent.click(within(dialog).getByRole("button", { name: "OK" }));
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  const menu = await openAccountMenu(members.alex.name);
  fireEvent.click(within(menu).getByRole("menuitem", { name: /^Settings/ }));
  dialog = await screen.findByRole("dialog", { name: "This isn't part of the Harmony prototype" });
  expect(dialog.textContent).toContain("Settings isn't included.");

  dispatcher.stop();
});

it("remembers the collapsed sidebar across a reload", async () => {
  const first = await renderLive(sessionAs(members.alex, 1));

  fireEvent.click(screen.getByRole("button", { name: "Close sidebar" }));
  expect(screen.getByRole("button", { name: "Open sidebar" })).toBeTruthy();
  const stored = window.localStorage.getItem("harmony.sidebar");
  first.dispatcher.stop();
  cleanup();

  // A reload: the store starts from its default (setting it writes storage too, so put
  // back what the toggle saved), then rehydrates from storage.
  sidebarStore.setState({ collapsed: false });
  window.localStorage.setItem("harmony.sidebar", stored ?? "");
  await sidebarStore.persist.rehydrate();
  const second = await renderLive(sessionAs(members.alex, 1));
  expect(screen.getByRole("button", { name: "Open sidebar" })).toBeTruthy();

  second.dispatcher.stop();
});

/** A window `width` px wide, as `matchMedia` answers the width queries the app asks. */
function windowWidth(width: number): void {
  vi.stubGlobal("matchMedia", (media: string) => {
    const [, side, px] = /\((min|max)-width:\s*(\d+)px\)/.exec(media) ?? [];
    const limit = Number(px);
    return {
      media,
      matches: side === "min" ? width >= limit : side === "max" ? width <= limit : false,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    };
  });
}

it("keeps the full sidebar from a 900px window", async () => {
  windowWidth(900);
  const { dispatcher } = await renderLive(sessionAs(members.alex, 1));
  expect(screen.getByRole("button", { name: "Close sidebar" })).toBeTruthy();
  expect(await screen.findByText(RECENT)).toBeTruthy();
  dispatcher.stop();
});

it("opens the network view from the demo bar, not the sidebar", async () => {
  const { dispatcher } = await renderLive(sessionAs(members.alex, 1));
  expect(screen.queryByRole("link", { name: "Network" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Network view" }));
  expect(await screen.findByText("Network at /network")).toBeTruthy();
  dispatcher.stop();
});

it("below 900px, shows the rail and opens the sidebar over the page until a link or Escape closes it", async () => {
  windowWidth(899);
  const { dispatcher } = await renderLive(sessionAs(members.alex, 1));

  // The stored choice is the full sidebar; there's no room for it beside the page.
  expect(screen.getByRole("button", { name: "Open sidebar" })).toBeTruthy();
  expect(screen.queryByText(RECENT)).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Open sidebar" }));
  expect(await screen.findByText(RECENT)).toBeTruthy();
  fireEvent.click(screen.getByRole("link", { name: /^Requests/ }));
  expect(await screen.findByText("Requests at /requests")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Open sidebar" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Open sidebar" }));
  fireEvent.keyDown(screen.getByRole("button", { name: "Close sidebar" }), { key: "Escape" });
  expect(screen.getByRole("button", { name: "Open sidebar" })).toBeTruthy();
  // Neither touched the member's stored choice.
  expect(sidebarStore.getState().collapsed).toBe(false);

  dispatcher.stop();
});

it("never stores the icon rail a page shows, so closing the tab there keeps the member's choice", () => {
  sidebarStore.setState({ collapsed: false });
  const hide = showRail();
  expect(JSON.parse(window.localStorage.getItem("harmony.sidebar") ?? "{}")).toMatchObject({
    state: { collapsed: false },
  });
  expect(window.localStorage.getItem("harmony.sidebar")).not.toContain("rail");
  // Opening the sidebar on that page opens it until the page closes.
  toggleSidebar();
  expect(sidebarStore.getState()).toMatchObject({ collapsed: false, rail: false });
  hide();
  expect(sidebarStore.getState()).toMatchObject({ collapsed: false, rail: false });
});

it("says when it can't reach Harmony, and recovers by itself", async () => {
  vi.spyOn(console, "warn").mockImplementation(() => {});
  demoStore.setState({ welcomed: true });
  const server = new FakeServer(sessionAs(members.alex, 1));
  server.down = true;
  const { dispatcher } = renderShell(server, { retryDelayMs: 20 });
  await act(() => dispatcher.start());

  expect((await screen.findByRole("alert")).textContent).toContain("Can't reach Harmony");
  expect(demoBar().textContent).toContain("Can't reach Harmony: retrying");

  server.down = false;
  await screen.findByRole("button", { name: "Account: Alex Chen" });
  expect(screen.queryByRole("alert")).toBeNull();

  dispatcher.stop();
});

it("says when live updates drop for longer than a blip, and clears once they're back", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] });
  const { sources, dispatcher } = await renderLive(sessionAs(members.alex, 1));
  const paused = "Live updates paused: reconnecting…";

  act(() => {
    sources.latest.drop();
  });
  expect(demoBar().textContent).not.toContain(paused);
  act(() => {
    vi.advanceTimersByTime(RECONNECT_GRACE_MS);
  });
  expect(within(demoBar()).getByRole("status").textContent).toContain(paused);

  act(() => {
    sources.latest.accept();
  });
  expect(demoBar().textContent).not.toContain(paused);
  // A second drop gets the same grace.
  act(() => {
    sources.latest.drop();
  });
  expect(demoBar().textContent).not.toContain(paused);

  vi.useRealTimers();
  dispatcher.stop();
});

// --- Requests in the sidebar ---------------------------------------------------------------

/** A request of Alex's that Ridgeline's question settled long ago, in his Done. */
function settled(index: number): InboxItem {
  const [asked] = alexInbox.needs_you;
  if (asked === undefined || asked.escalation === null) throw new Error("no question of Alex's");
  return {
    ...asked,
    thread_id: `0b1d7a3e-0000-4000-8000-${String(400 + index).padStart(12, "0")}`,
    title: `Settled request ${String(index)}`,
    thread_state: "closed",
    escalation: { ...asked.escalation, status: "resolved" },
    last_activity_at: new Date(Date.parse(FIXTURE_NOW) - (60 + index) * 60_000).toISOString(),
  };
}

it("lists only what needs the member and All requests above Recents, in one scroll area", async () => {
  const { server, dispatcher, queryClient } = await renderLive(sessionAs(members.alex, 1));
  server.alexInbox = { ...alexInbox, done: Array.from({ length: 12 }, (_, index) => settled(index)) };
  await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.inbox }));

  const needs = await screen.findByRole("region", { name: "Needs you" });
  const chats = screen.getByRole("region", { name: "Recents" });
  expect(needs.compareDocumentPosition(chats) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const scroller = needs.closest(".overflow-y-auto");
  expect(scroller).not.toBeNull();
  expect(chats.closest(".overflow-y-auto")).toBe(scroller);

  // Settled requests live on the Requests page, not in the sidebar.
  expect(screen.queryByRole("region", { name: "Recent requests" })).toBeNull();
  expect(screen.queryByText("Settled request 0")).toBeNull();
  expect(within(needs).getAllByRole("link").at(-1)?.getAttribute("href")).toBe("/requests");

  // Alex's own side asked this one: he answers it in the chat that sent the request.
  const asked = within(needs).getByRole("link", { name: /Stacked CT holder for 12 lattice coupons/ });
  expect(asked.getAttribute("href")).toBe(`/chat/${QUESTION_CHAT}`);

  fireEvent.click(asked);
  expect(await screen.findByText(`Chat at /chat/${QUESTION_CHAT}`)).toBeTruthy();

  dispatcher.stop();
});

it("opens Helen's items in Requests, and leaves only the badge on the rail", async () => {
  const { dispatcher } = await renderLive(sessionAs(members.helen, 1));
  const needs = await screen.findByRole("region", { name: "Needs you" });
  // All requests sits under what needs her.
  expect(within(needs).getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual([
    `/requests/${THREAD(1)}`,
    `/requests/${THREAD(2)}`,
    "/requests",
  ]);
  expect(screen.queryByRole("region", { name: "Recent requests" })).toBeNull();
  // Neither has been opened in this browser: each link says it's unread, not a bare dot.
  expect(within(needs).getAllByRole("link", { name: /, unread$/ })).toHaveLength(2);
  expect(within(needs).queryByLabelText("Unread")).toBeNull();
  fireEvent.click(within(needs).getByRole("link", { name: /Thursday CT availability/ }));
  expect(await screen.findByText(`Thread at /requests/${THREAD(2)}`)).toBeTruthy();

  act(() => {
    toggleSidebar();
  });
  expect(screen.queryByRole("region", { name: "Needs you" })).toBeNull();
  expect(screen.getByLabelText("2 need you")).toBeTruthy();

  dispatcher.stop();
});
