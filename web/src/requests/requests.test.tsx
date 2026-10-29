// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import { queryKeys } from "../api/queryKeys";
import type { ReopenBlock, SteerView } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import { fetchStreamSnapshot } from "../app/snapshot";
import { stoppedLine } from "../chat/failureCopy";
import { sharingPath } from "../customize/sections";
import { EARLIER_ID, OTHERS_THREAD, whoseRoutes } from "../demo/fixtures";
import { fixtureFetch, problem, type FixtureRoutes } from "../fixtures";
import {
  EDIT_INSTRUCTION,
  ESCALATION,
  HENDERSON_QUESTION,
  HENDERSON_REWRITE,
  HENDERSON_SHORT_QUESTION,
  REOPEN_INSTRUCTION,
  THREAD,
  VANTAGE_REPLY,
  checking,
  edited,
  ended,
  hendersonDraft,
  reopenFailed,
  reopenOffer,
  reopened,
  requestsRoutes,
  requestsWorld,
  rewritten,
  routedToRaj,
  waitingOnThem,
  type RequestsRouteOptions,
} from "../fixtures/requests";
import { members, nonAdmin, orgs } from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { durable, ephemeral, EventSources, worldScope } from "../realtime/testing";
import { shortOrgName } from "../shell/names";
import { toastStore } from "../shell/toasts";
import { TooltipProvider } from "../shell/ui";
import { RequestsPage } from "./RequestsPage";
import { SidebarRequests } from "./SidebarRequests";
import { AUTONOMY_PATH } from "./decision";
import { blockLine, reopenFailure } from "./failure";
import { REDRAFT_TIMEOUT_MS } from "./useDecision";

interface Call {
  route: string;
  body: unknown;
}

interface RenderOptions extends RequestsRouteOptions {
  /** Routes that replace the fixtures' own. */
  overrides?: FixtureRoutes;
  /** Start the event stream, so the session (and with it the budget) loads. */
  live?: boolean;
  /** Writes whose first call gets this answer instead of the fixtures': "drop" gets none at all. */
  first?: Readonly<Record<string, Response | "drop">>;
  /** The sidebar's requests beside the page. */
  sidebar?: boolean;
}

/** `/requests/:threadId?` over the requests fixtures, recording every write. */
function renderRequests(
  threadId: string | null,
  { overrides, live = false, first = {}, sidebar = false, ...options }: RenderOptions = {},
) {
  const serve = fixtureFetch({ ...requestsRoutes({ world: requestsWorld(), ...options }), ...whoseRoutes, ...overrides });
  const writes: Call[] = [];
  const answers = new Map(Object.entries(first));
  const api = createApiClient({
    baseUrl: "http://harmony.test",
    fetch: async (request) => {
      if (request.method !== "GET") {
        const text = await request.clone().text();
        const route = `${request.method} ${new URL(request.url).pathname}`;
        writes.push({ route, body: text === "" ? undefined : (JSON.parse(text) as unknown) });
        const answer = answers.get(route);
        if (answer !== undefined) {
          answers.delete(route);
          if (answer === "drop") throw new TypeError("Failed to fetch");
          return answer;
        }
      }
      return serve(request);
    },
  });
  const queryClient = createQueryClient();
  const sources = new EventSources();
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
    openEventSource: sources.open,
    openChannel: () => null,
    reload: vi.fn(),
  });
  if (live) void dispatcher.start();
  const page = sidebar ? (
    <>
      <nav aria-label="Sidebar">
        <SidebarRequests />
      </nav>
      <RequestsPage />
    </>
  ) : (
    <RequestsPage />
  );
  const router = createMemoryRouter([{ path: "/requests/:threadId?", element: page }], {
    initialEntries: [threadId === null ? "/requests" : `/requests/${threadId}`],
  });
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </AppProviders>,
  );
  return { writes, router, sources, queryClient };
}

/** The view once the session, and with it the budget, has loaded. */
async function renderLive(threadId: string, options: RenderOptions = {}) {
  const rendered = renderRequests(threadId, { ...options, live: true });
  await waitFor(() => {
    expect(rendered.sources.opened).toHaveLength(1);
  });
  act(() => {
    rendered.sources.latest.accept();
  });
  return rendered;
}

const card = () => screen.findByRole("region", { name: "Needs your decision" });
/** The view's one box, for a comment or an Edit. */
const box = () => screen.findByRole("region", { name: "Comment or Edit" });
const conversation = () => screen.findByRole("list", { name: "Conversation" });
const field = (region: HTMLElement) => within(region).getByRole<HTMLTextAreaElement>("textbox");
const button = (region: HTMLElement, name: string) => within(region).getByRole<HTMLButtonElement>("button", { name });
const draft = (region: HTMLElement, name = "Draft reply") => within(region).getByRole<HTMLTextAreaElement>("textbox", { name });
/** The card's sticky foot: Decline and Approve and send, or the Decline step. */
const foot = (region: HTMLElement) => {
  const found = region.querySelector<HTMLElement>("[data-card-foot]");
  if (found === null) throw new Error("no card foot");
  return found;
};
/** The latest run's Closed row. */
const closedRow = async () => {
  const latest = (await screen.findAllByRole("region", { name: /^Closed / })).at(-1);
  if (latest === undefined) throw new Error("no Closed row");
  return latest;
};

const resolveRoute = (escalationId: string) => `POST /api/escalations/${escalationId}/resolve`;
const notesRoute = (threadId: string) => `POST /api/threads/${threadId}/notes`;
const steersRoute = (threadId: string) => `POST /api/threads/${threadId}/steers`;
const reopenRoute = (threadId: string) => `POST /api/threads/${threadId}/reopen`;
const HENDERSON_ANSWER = hendersonDraft("").answer;
const raj = { id: members.raj.id, slug: members.raj.slug, name: members.raj.name };

function type(region: HTMLElement, value: string) {
  fireEvent.change(field(region), { target: { value } });
}

function mode(region: HTMLElement, name: "Comment" | "Edit") {
  return within(within(region).getByRole("radiogroup", { name: "Mode" })).getByRole("radio", { name });
}

/** Switches the box to Edit and types `text`, then sends it unless told not to. */
async function tellClaude(text: string, send = true) {
  const region = await box();
  fireEvent.click(mode(region, "Edit"));
  type(region, text);
  if (send) fireEvent.click(button(region, "Tell Claude"));
  return region;
}

/** The `client_key` of each write to `route`, in order. */
function keysOf(writes: readonly Call[], route: string): unknown[] {
  return writes.filter((write) => write.route === route).map((write) => (write.body as { client_key?: unknown }).client_key);
}

/** Opens the Reopen prompt on the latest Closed row and types `text`. */
async function openReopen(text: string) {
  fireEvent.click(within(await closedRow()).getByRole("button", { name: "Reopen" }));
  const prompt = await screen.findByRole("region", { name: `Reopen with ${orgs.ridgeline.name}` });
  fireEvent.change(within(prompt).getByRole("textbox", { name: "What should Claude pick up?" }), { target: { value: text } });
  return prompt;
}

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
  // jsdom has no scrolling; the transcript pins itself to its end.
  Element.prototype.scrollTo = () => undefined;
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the list", () => {
  it("lists every request under a serif heading, by search and by direction, with no list beside a thread", async () => {
    renderRequests(null);
    const heading = await screen.findByRole("heading", { level: 1, name: "Requests" });
    expect(heading.className).toContain("font-serif");
    const needs = await screen.findByRole("region", { name: /^Needs you/ });
    const henderson = within(needs).getByRole("link", { name: /CT and compression quote/ });
    expect(henderson.getAttribute("href")).toBe(`/requests/${THREAD.henderson}`);
    expect(henderson.textContent).toMatch(/Henderson Frame Lab · Needs your answer · /);
    // Helen hasn't opened it in this browser: the link's name says so, not just a dot.
    expect(within(needs).getByRole("link", { name: /^CT and compression quote.*, unread$/ })).toBe(henderson);
    expect(within(needs).queryByLabelText("Unread")).toBeNull();
    expect(screen.getByRole("region", { name: /^Done/ })).toBeTruthy();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search requests" }), { target: { value: "tubes" } });
    expect(screen.queryByRole("region", { name: /^Needs you/ })).toBeNull();
    const found = screen.getAllByRole("link");
    expect(found).toHaveLength(1);
    expect(found[0]?.textContent).toContain("CT on machined holder tubes");

    fireEvent.change(screen.getByRole("searchbox", { name: "Search requests" }), { target: { value: "" } });
    const show = screen.getByRole("radiogroup", { name: "Show" });
    // Every one of Helen's requests came to Northlake.
    fireEvent.click(within(show).getByRole("radio", { name: "Outbound" }));
    expect(screen.getByText("No matching requests")).toBeTruthy();
    fireEvent.click(within(show).getByRole("radio", { name: "Inbound" }));
    expect(await screen.findByRole("region", { name: /^Needs you/ })).toBeTruthy();
    cleanup();

    renderRequests(THREAD.henderson);
    await card();
    expect(screen.queryByRole("complementary", { name: "Requests list" })).toBeNull();
    expect(screen.queryByRole("searchbox")).toBeNull();
  });

  it("marks an unread request in the sidebar with one dot, at the row's end, until it's opened", async () => {
    const { router } = renderRequests(THREAD.pacific, { sidebar: true });
    const sidebar = await screen.findByRole("navigation", { name: "Sidebar" });
    const unread = await within(sidebar).findByRole("link", { name: "CT and compression quote, with Henderson Frame Lab, unread" });
    const dots = () => unread.querySelectorAll("[class*='rounded-full']");
    expect(dots()).toHaveLength(1);
    expect(unread.lastElementChild).toBe(dots()[0]);

    await act(() => router.navigate(`/requests/${THREAD.henderson}`));
    await card();
    const read = await within(sidebar).findByRole("link", { name: "CT and compression quote, with Henderson Frame Lab" });
    expect(read.querySelectorAll("[class*='rounded-full']")).toHaveLength(0);
  });
});

describe("the view", () => {
  it("heads the request with its title, the counterparty, a pill that keeps its width, and Details", async () => {
    renderRequests(THREAD.henderson);
    const title = await screen.findByRole("heading", { level: 1, name: "CT and compression quote" });
    // The title truncates first; the pill never gives way.
    expect(title.className).toMatch(/\btruncate\b/);
    expect(screen.getByText("with Henderson Frame Lab")).toBeTruthy();
    const pill = screen.getByText("Needs your answer");
    expect(pill.className).toMatch(/\bshrink-0\b/);
    expect(pill.className).not.toMatch(/\bmin-w-0\b/);
    expect(screen.getByRole("button", { name: "Details" }).getAttribute("aria-expanded")).toBe("false");
  });

  it('lets a status that names the counterparty stand for "with" them', async () => {
    renderRequests(THREAD.pacific, { world: waitingOnThem(requestsWorld(), THREAD.pacific) });
    await screen.findByRole("heading", { level: 1, name: "Scouting CT of printed nodes" });
    expect(screen.getByText(`Waiting on ${orgs.pacific.name}`)).toBeTruthy();
    expect(screen.queryByText(`with ${orgs.pacific.name}`)).toBeNull();
  });

  it("keeps Details closed until asked, then holds Share, what our representative saw, guidance, Properties and Activity", async () => {
    renderRequests(THREAD.henderson);
    await card();
    expect(screen.queryByRole("complementary", { name: "Properties" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    const details = screen.getByRole("complementary", { name: "Properties" });
    expect(within(details).getByRole("button", { name: "Share" })).toBeTruthy();
    expect(within(details).getByRole("button", { name: /^What our representative saw/ })).toBeTruthy();
    const guidance = within(details).getByRole("region", { name: "Guidance Claude follows" });
    expect(guidance.textContent).toContain("None yet. Use Edit to tell Claude what to do differently.");
    expect(within(details).getByRole("heading", { name: "Properties" })).toBeTruthy();
    expect(within(details).getByRole("heading", { name: "Activity" })).toBeTruthy();
    expect(within(details).getByText("Henderson Frame Lab")).toBeTruthy();
    cleanup();

    // Each viewer's choice is remembered.
    renderRequests(THREAD.henderson);
    expect(await screen.findByRole("complementary", { name: "Properties" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close details" }));
    expect(screen.queryByRole("complementary", { name: "Properties" })).toBeNull();
    cleanup();

    renderRequests(THREAD.henderson);
    await card();
    expect(screen.queryByRole("complementary", { name: "Properties" })).toBeNull();
  });

  it("gives focus back to what opened Details, Share and what our representative saw when they close", async () => {
    renderRequests(THREAD.henderson);
    await card();
    const toggle = screen.getByRole("button", { name: "Details" });
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Close details" }));
    expect(document.activeElement).toBe(toggle);

    fireEvent.click(toggle);
    const details = screen.getByRole("complementary", { name: "Properties" });
    const share = within(details).getByRole("button", { name: "Share" });
    fireEvent.click(share);
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Share with a teammate" })).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(document.activeElement).toBe(share);
    });

    const saw = within(details).getByRole("button", { name: /^What our representative saw/ });
    fireEvent.click(saw);
    const drawer = await screen.findByRole("dialog", { name: "What our representative saw" });
    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(document.activeElement).toBe(saw);
    });
  });

  it("opens Details as a sheet that starts below the demo bar when the view is narrow", async () => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 700, 800));
    renderRequests(THREAD.henderson);
    await card();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    const sheet = await screen.findByRole("dialog", { name: "Details" });
    expect(sheet.className).toContain("top-[calc(var(--demo-bar-height)+8px)]");
    expect(screen.queryByRole("complementary", { name: "Properties" })).toBeNull();
  });

  it("opens what our representative saw from Details, which no comment reached", async () => {
    renderRequests(THREAD.henderson);
    await card();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    fireEvent.click(screen.getByRole("button", { name: /^What our representative saw/ }));
    const drawer = await screen.findByRole("dialog", { name: "What our representative saw" });
    const prompt = await within(drawer).findByRole("region", { name: "Its prompt" });
    expect(prompt.textContent).toContain("What it may pass on");
    expect(drawer.textContent).not.toContain("1 to 5 works");
  });

  it("says whose a request is when the viewer can't open it, and when it's from an earlier demo", async () => {
    renderRequests(OTHERS_THREAD);
    expect(await screen.findByText("Helen can't open this request")).toBeTruthy();
    cleanup();

    renderRequests(EARLIER_ID);
    expect(await screen.findByText("This request isn't in this demo")).toBeTruthy();
  });

  it("shows every message in full, however long, with nothing to expand", async () => {
    const world = requestsWorld();
    const last = world.threads.get(THREAD.pacific)?.detail.messages.at(-1);
    if (last === undefined) throw new Error("no Pacific message");
    const long = Array.from({ length: 40 }, (_, index) => `Line ${String(index + 1)} of the beam schedule.`).join("\n");
    last.body = long;
    renderRequests(THREAD.pacific, { world });
    const list = await conversation();
    const shown = [...list.querySelectorAll("p")].find((element) => element.textContent === long);
    expect(shown).toBeDefined();
    expect(shown?.className).not.toMatch(/line-clamp|max-h-|overflow-hidden/);
    expect(screen.queryByRole("button", { name: /more/i })).toBeNull();
  });

  it("shows no routing part, no reasoning and no row for our Claude asking the team", async () => {
    renderRequests(THREAD.henderson);
    const decision = await card();
    const list = await conversation();
    expect(within(decision).queryByRole("region", { name: /^Why it came to/ })).toBeNull();
    expect(decision.textContent).not.toMatch(/Routed to/);
    expect(within(decision).queryByRole("button", { name: /reasoning/i })).toBeNull();
    expect(list.textContent).not.toContain(HENDERSON_QUESTION);
  });

  it("shows the team's comments inline, apart from the messages", async () => {
    renderRequests(THREAD.henderson);
    const list = await conversation();
    const comment = within(list).getByRole("article", { name: "Comment from Raj Patel" });
    expect(comment.textContent).toMatch(/^Raj Patel · comment · /);
    expect(comment.textContent).toContain("1 to 5 works");
    expect(comment.className).toContain("border-caution");
  });
});

describe("whose move it is", () => {
  const delta = (org: string, text: string, turn = "0b1d7a3e-0000-4000-8000-0000000009f1") =>
    ephemeral({
      type: "stream.delta",
      org_id: org,
      thread_id: THREAD.pacific,
      turn_id: turn,
      attempt: 1,
      text,
    });

  it("says it's their move in one steady row, ignores their Claude writing, and gives way to ours", async () => {
    const { sources } = await renderLive(THREAD.pacific, { world: waitingOnThem(requestsWorld(), THREAD.pacific) });
    const list = await conversation();
    const waiting = "Waiting for Pacific Beamline to reply";
    expect(within(list).getByText(waiting)).toBeTruthy();

    act(() => {
      sources.latest.send(delta(orgs.pacific.id, "Checking our beam schedule"));
    });
    expect(within(list).queryByRole("article", { name: /is writing$/ })).toBeNull();
    expect(list.textContent).not.toContain("Checking our beam schedule");
    expect(within(list).getByText(waiting)).toBeTruthy();

    act(() => {
      sources.latest.send(delta(orgs.northlake.id, "Checking the CT calendar"));
    });
    const ours = await within(list).findByRole("article", { name: "Claude for Northlake Characterization Core is writing" });
    const text = within(ours).getByText("Checking the CT calendar");
    expect(text.className).toContain("font-serif");
    expect(text.className).not.toMatch(/\b(bg-|border|rounded)/);
    // Never both: the waiting row would contradict the row being written.
    expect(within(list).queryByText(waiting)).toBeNull();
    // Only the fixed line is announced, never the growing text.
    expect(screen.getAllByRole("status").map((status) => status.textContent)).toContain(
      "Claude for Northlake Characterization Core is writing…",
    );
  });

  it("drops a turn's last delta that lands after its committed message, and shows the next turn", async () => {
    const { sources } = await renderLive(THREAD.pacific, { world: waitingOnThem(requestsWorld(), THREAD.pacific) });
    const list = await conversation();
    const send = (envelope: Parameters<typeof sources.latest.send>[0]) => {
      act(() => {
        sources.latest.send(envelope);
      });
    };
    send(delta(orgs.northlake.id, "Thursday afternoons aren't available, "));
    await within(list).findByRole("article", { name: /is writing$/ });

    send(
      durable(
        10_001,
        {
          type: "entity.snapshot",
          entity_type: "thread_message",
          entity_id: "0b1d7a3e-0000-4000-8000-0000000009f2",
          version: 1,
          data: { thread_id: THREAD.pacific },
        },
        { ...worldScope, kind: "thread", org_id: orgs.northlake.id, thread_id: THREAD.pacific },
      ),
    );
    await waitFor(() => {
      expect(within(list).queryByRole("article", { name: /is writing$/ })).toBeNull();
    });
    send(delta(orgs.northlake.id, "but another weekday works."));
    expect(within(list).queryByRole("article", { name: /is writing$/ })).toBeNull();
    expect(list.textContent).not.toContain("but another weekday works.");

    send(delta(orgs.northlake.id, "Following up", "0b1d7a3e-0000-4000-8000-0000000009f3"));
    await within(list).findByRole("article", { name: /is writing$/ });
  });

  it("says our Claude is checking our records while it waits on our side", async () => {
    renderRequests(THREAD.pacific, { world: checking(requestsWorld(), THREAD.pacific) });
    const list = await conversation();
    expect(within(list).getByText("Checking Northlake Characterization Core's records…")).toBeTruthy();
  });

  it("names whose answer it waits on to anyone but the assignee, who has the card", async () => {
    renderRequests(THREAD.henderson, { as: members.raj });
    const decision = await card();
    expect(within(await conversation()).getByText("Waiting on Dr. Helen Ward")).toBeTruthy();
    // Raj may still act on it; the card says whose it is.
    expect(within(decision).getByText("Claude needs Helen's answer before replying to Henderson Frame Lab")).toBeTruthy();
    cleanup();

    renderRequests(THREAD.henderson);
    await card();
    expect(within(await conversation()).queryByText(/^Waiting on/)).toBeNull();
  });
});

describe("the decision card", () => {
  it("reads after the transcript: whose it is, the question, why it waits, the whole draft, and two buttons", async () => {
    renderRequests(THREAD.henderson);
    const decision = await card();
    expect(within(decision).getByText("Claude needs your answer before replying to Henderson Frame Lab")).toBeTruthy();
    expect(within(decision).getByRole("heading", { level: 2 }).textContent).toBe(HENDERSON_SHORT_QUESTION);
    expect(
      within(decision).getByText(/Northlake shares what's open on the public calendar, not other clients or their bookings\.$/),
    ).toBeTruthy();
    expect(button(decision, "Reassign")).toBeTruthy();

    expect(draft(decision).value).toBe(HENDERSON_ANSWER);
    expect(within(decision).getByText("Click the text to change the wording")).toBeTruthy();
    const lookedAt = within(decision).getByRole("region", { name: "What Claude looked at" });
    const line = within(lookedAt).getByRole("button", { name: /^Looked at: / });
    expect(line.textContent).toBe(
      "Looked at: Compression availability before Nov 6 · Public calendar · Raj's note, Nov 4 compression block · Price list, external academic",
    );
    fireEvent.click(line);
    expect(within(lookedAt).getAllByRole("listitem")).toHaveLength(4);
    const link = within(decision).getByRole("link", { name: "Edit what you share with Henderson Frame Lab" });
    expect(link.getAttribute("href")).toBe(sharingPath(orgs.henderson.id));

    // Only Decline and Approve and send, in a foot that sticks while the card is taller than the view.
    const actions = foot(decision);
    expect(decision.lastElementChild).toBe(actions);
    expect(actions.className).toMatch(/\bsticky\b/);
    expect(within(actions).getAllByRole("button").map((item) => item.textContent)).toEqual(["Decline", "Approve and send"]);
    expect(actions.textContent).toContain("Claude replies to Henderson Frame Lab with this answer · ⌘↵");
    expect(decision.className).not.toMatch(/overflow-y-auto|max-h-/);

    const list = await conversation();
    expect(list.compareDocumentPosition(decision) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(decision.contains(await box())).toBe(false);
  });

  it("titles the item with the representative's whole question when the answerer gave no short one", async () => {
    const world = requestsWorld();
    const item = world.threads.get(THREAD.henderson)?.workspace.escalations[0];
    if (item === undefined) throw new Error("no Henderson item");
    item.drafts = item.drafts.map((rev) => ({ ...rev, short_question: null }));
    renderRequests(THREAD.henderson, { world });
    expect(within(await card()).getByRole("heading", { level: 2 }).textContent).toBe(HENDERSON_QUESTION);
  });

  it("lets a non-admin see, not edit, what our side shares", async () => {
    renderRequests(THREAD.henderson, { as: nonAdmin(members.helen) });
    const decision = await card();
    expect(within(decision).getByRole("link", { name: "See what you share with Henderson Frame Lab" })).toBeTruthy();
    expect(within(decision).queryByRole("link", { name: /^Edit what you share/ })).toBeNull();
  });

  it("asks whether to send a held message, saying which mode held it, and sends it with its rev", async () => {
    const { writes } = renderRequests(THREAD.vantage);
    const decision = await card();
    expect(within(decision).getByText("Claude wants to send this to Vantage Market Intelligence")).toBeTruthy();
    expect(within(decision).getByRole("heading", { level: 2 }).textContent).toBe(
      "Send this reply to Vantage Market Intelligence?",
    );
    expect(draft(decision, "Claude's message").value).toBe(VANTAGE_REPLY);
    // Ask mode is the only reason, and the foot says it.
    expect(decision.textContent).not.toContain("A teammate approves every message in Ask mode.");
    const actions = foot(decision);
    expect(await within(actions).findByText(/^Ask mode with Vantage Market Intelligence ·/)).toBeTruthy();
    expect(within(actions).getByRole("link", { name: "Change" }).getAttribute("href")).toBe(AUTONOMY_PATH);
    expect(actions.textContent).toContain("Sends this message to Vantage Market Intelligence as shown");

    fireEvent.click(button(actions, "Approve and send"));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    expect(writes).toEqual([
      { route: resolveRoute(ESCALATION.vantage), body: { action: "approve", text: VANTAGE_REPLY, draft_rev: 1 } },
    ]);
    expect(await within(await conversation()).findByText(VANTAGE_REPLY)).toBeTruthy();
  });

  it("approves the wording as edited where it stands, and Undo my edits puts Claude's back", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const decision = await card();
    fireEvent.change(draft(decision), { target: { value: "Yes, Wednesday." } });
    expect(within(decision).getByText("· edited by you")).toBeTruthy();
    expect(foot(decision).textContent).toContain("Claude replies to Henderson Frame Lab with your edited answer");
    fireEvent.click(button(decision, "Undo my edits"));
    expect(draft(decision).value).toBe(HENDERSON_ANSWER);
    expect(within(decision).queryByText("· edited by you")).toBeNull();

    fireEvent.change(draft(decision), { target: { value: "Yes, Wednesday." } });
    fireEvent.click(button(decision, "Approve and send"));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    expect(writes).toEqual([
      { route: resolveRoute(ESCALATION.henderson), body: { action: "approve", text: "Yes, Wednesday.", draft_rev: 1 } },
    ]);
  });

  it("pages through Claude's rewrites, with Undo my edits before the pager, and approves the rev shown", async () => {
    const { writes } = renderRequests(THREAD.henderson, { world: rewritten(requestsWorld(), THREAD.henderson) });
    const decision = await card();
    expect(within(decision).getByText("· rewritten by Claude")).toBeTruthy();
    expect(within(decision).getByText("2 of 2")).toBeTruthy();
    expect(draft(decision).value).toBe(HENDERSON_REWRITE);

    fireEvent.click(button(decision, "Earlier version"));
    expect(within(decision).getByText("1 of 2")).toBeTruthy();
    expect(within(decision).queryByText("· rewritten by Claude")).toBeNull();
    expect(draft(decision).value).toBe(HENDERSON_ANSWER);

    fireEvent.change(draft(decision), { target: { value: "Yes, Thursday." } });
    const undo = button(decision, "Undo my edits");
    expect(undo.compareDocumentPosition(button(decision, "Later version")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(button(decision, "Approve and send"));
    await waitFor(() => {
      expect(writes).toEqual([
        { route: resolveRoute(ESCALATION.henderson), body: { action: "approve", text: "Yes, Thursday.", draft_rev: 1 } },
      ]);
    });
  });

  it("approves on ⌘↵ only with focus in the card; Enter in the box never approves", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const decision = await card();
    const region = await box();
    fireEvent.keyDown(field(region), { key: "Enter" });
    fireEvent.keyDown(field(region), { key: "Enter", metaKey: true });
    fireEvent.keyDown(document.body, { key: "Enter", metaKey: true });
    expect(writes).toEqual([]);

    fireEvent.keyDown(draft(decision), { key: "Enter", metaKey: true });
    await waitFor(() => {
      expect(writes).toEqual([
        { route: resolveRoute(ESCALATION.henderson), body: { action: "approve", text: HENDERSON_ANSWER, draft_rev: 1 } },
      ]);
    });
  });

  it("declines in a step of its own that needs a reason, and Cancel brings the buttons back", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const decision = await card();
    fireEvent.click(button(decision, "Decline"));
    const step = within(decision).getByRole("region", { name: "Decline to answer" });
    expect(step.textContent).toContain("Claude tells Henderson Frame Lab you can't answer, and may pass on your reason.");
    expect(button(step, "Decline").disabled).toBe(true);
    fireEvent.click(button(step, "Cancel"));
    expect(button(foot(decision), "Approve and send")).toBeTruthy();

    fireEvent.click(button(decision, "Decline"));
    const again = within(decision).getByRole("region", { name: "Decline to answer" });
    fireEvent.change(within(again).getByRole("textbox", { name: "Reason" }), {
      target: { value: "The reserve block is for internal work." },
    });
    fireEvent.click(button(again, "Decline"));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    expect(writes).toEqual([
      {
        route: resolveRoute(ESCALATION.henderson),
        body: { action: "decline", text: "The reserve block is for internal work.", draft_rev: 1 },
      },
    ]);
  });

  it("reassigns from the eyebrow line, then names the new assignee", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const decision = await card();
    fireEvent.click(button(decision, "Reassign"));
    const picker = await screen.findByRole("listbox", { name: "Reassign to" });
    const rajOption = await within(picker).findByRole<HTMLButtonElement>("option", { name: /Raj Patel/ });
    const helen = within(picker).getByRole<HTMLButtonElement>("option", { name: /Helen Ward/ });
    expect(helen.getAttribute("aria-selected")).toBe("true");
    expect(helen.disabled).toBe(true);
    fireEvent.click(rajOption);
    await waitFor(() => {
      expect(writes).toEqual([
        { route: `POST /api/escalations/${ESCALATION.henderson}/reassign`, body: { member_id: members.raj.id } },
      ]);
    });
    expect(await within(decision).findByText("Claude needs Raj's answer before replying to Henderson Frame Lab")).toBeTruthy();
    expect(within(await conversation()).getByText("Waiting on Raj Patel")).toBeTruthy();
  });

  it("takes a member who reassigns away their only way in back to Requests", async () => {
    toastStore.setState({ toasts: [] });
    const { writes, router } = renderRequests(THREAD.henderson, {
      as: nonAdmin(members.raj),
      world: routedToRaj(requestsWorld()),
    });
    fireEvent.click(button(await card(), "Reassign"));
    const picker = await screen.findByRole("listbox", { name: "Reassign to" });
    fireEvent.click(await within(picker).findByRole("option", { name: /Helen Ward/ }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/requests");
    });
    expect(writes).toEqual([
      { route: `POST /api/escalations/${ESCALATION.henderson}/reassign`, body: { member_id: members.helen.id } },
    ]);
    expect(toastStore.getState().toasts.map((toast) => toast.text)).toEqual([
      "Reassigned to Helen. It's no longer in your Requests.",
    ]);
  });

  it("says a request someone else took away is no longer visible, and keeps focus in the page", async () => {
    const world = routedToRaj(requestsWorld());
    const { router, queryClient } = renderRequests(THREAD.henderson, { as: nonAdmin(members.raj), world });
    await card();
    const record = world.threads.get(THREAD.henderson);
    if (record !== undefined) record.detail.access = record.detail.access.filter((row) => row.member_id !== members.raj.id);
    await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.thread(THREAD.henderson) }));

    const back = await screen.findByRole("button", { name: "Back to Requests" });
    expect(screen.getByText("This request is no longer visible to you")).toBeTruthy();
    expect(document.activeElement).toBe(back);
    fireEvent.click(back);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/requests");
    });
  });

  it("says who got there first when the item was already resolved", async () => {
    renderRequests(THREAD.henderson, { resolvedFirstBy: members.raj });
    const decision = await card();
    fireEvent.click(button(decision, "Approve and send"));
    expect((await within(decision).findByRole("status")).textContent).toBe("Already resolved by Raj Patel.");
  });

  it("doesn't name a teammate when the item changed without anyone resolving it", async () => {
    renderRequests(THREAD.henderson, {
      overrides: {
        "POST /api/escalations/{escalation_id}/resolve": () =>
          problem(409, "escalation_not_open", "escalation is cancelled", { resolved_by: null }),
      },
    });
    const decision = await card();
    fireEvent.click(button(decision, "Approve and send"));
    expect((await within(decision).findByRole("status")).textContent).toMatch(/^This item changed while you were deciding/);
  });
});

describe("the box", () => {
  it("starts in Comment, for the team, and switches to Edit, for our Claude", async () => {
    renderRequests(THREAD.henderson);
    const decision = await card();
    const region = await box();
    expect(within(region).getAllByRole("textbox")).toHaveLength(1);
    expect(mode(region, "Comment").getAttribute("aria-checked")).toBe("true");
    expect(field(region).getAttribute("aria-label")).toBe("Comment");
    expect(field(region).placeholder).toBe("Comment for your team… Use @ to bring someone in");
    expect(button(region, "Add comment").disabled).toBe(true);
    expect(screen.getByText("Only Northlake sees comments. Claude doesn't read them.")).toBeTruthy();

    fireEvent.click(mode(region, "Edit"));
    expect(mode(region, "Edit").getAttribute("aria-checked")).toBe("true");
    expect(field(region).getAttribute("aria-label")).toBe("Tell Claude");
    expect(field(region).placeholder).toBe("What should Claude do differently?");
    expect(button(region, "Tell Claude").disabled).toBe(true);
    expect(
      screen.getByText("Claude rewrites the draft and follows this for the rest of the conversation. Only Northlake sees it."),
    ).toBeTruthy();

    // Typed but not sent: the card marks the draft Claude will rewrite, and warns no further.
    type(region, EDIT_INSTRUCTION);
    expect(within(decision).getByText("Claude rewrites this when you tell it")).toBeTruthy();
    expect(within(decision).queryByText(/isn't sent yet/)).toBeNull();
    fireEvent.click(mode(region, "Comment"));
    expect(field(region).value).toBe(EDIT_INSTRUCTION);
    expect(within(decision).queryByText("Claude rewrites this when you tell it")).toBeNull();
  });

  it("says an Edit with no draft waiting changes what Claude does from its next message", async () => {
    renderRequests(THREAD.pacific, { world: waitingOnThem(requestsWorld(), THREAD.pacific) });
    fireEvent.click(mode(await box(), "Edit"));
    expect(screen.getByText("Claude follows this from its next message in this conversation. Only Northlake sees it.")).toBeTruthy();
  });

  it("posts a comment as a note only, for the team", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const region = await box();
    type(region, "Check with Raj first.");
    fireEvent.keyDown(field(region), { key: "Enter" });
    await waitFor(() => {
      expect(writes).toEqual([
        { route: notesRoute(THREAD.henderson), body: { body: "Check with Raj first.", as_member_id: members.helen.id } },
      ]);
    });
    const comments = within(await conversation()).getAllByRole("article", { name: "Comment from Dr. Helen Ward" });
    await waitFor(() => {
      expect(comments.at(-1)?.textContent).toContain("Check with Raj first.");
    });
    await waitFor(() => {
      expect(field(region).value).toBe("");
    });
    expect(await card()).toBeTruthy();
  });

  it("tells Claude through an Edit only, which rewrites the waiting draft, then goes back to Comment", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const decision = await card();
    const region = await tellClaude(EDIT_INSTRUCTION);

    expect(await within(decision).findByText("2 of 2")).toBeTruthy();
    expect(within(decision).getByText("· rewritten by Claude")).toBeTruthy();
    expect(draft(decision).value).toBe(HENDERSON_REWRITE);
    expect(writes).toEqual([
      {
        route: steersRoute(THREAD.henderson),
        body: {
          client_key: expect.any(String) as unknown,
          body: EDIT_INSTRUCTION,
          escalation_id: ESCALATION.henderson,
          draft_text: null,
          as_member_id: members.helen.id,
        },
      },
    ]);
    expect(mode(region, "Comment").getAttribute("aria-checked")).toBe("true");
    expect(field(region).value).toBe("");

    const row = within(await conversation()).getByRole("article", { name: "Helen told Claude what to do differently" });
    expect(row.textContent).toContain("only Northlake sees this");
    expect(row.textContent).toContain(`“${EDIT_INSTRUCTION}”`);
    expect(row.textContent).toContain("Claude rewrote the draft and will follow this for the rest of the conversation.");
  });

  it("sends the member's own wording of the draft for Claude to rewrite from", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const decision = await card();
    fireEvent.change(draft(decision), { target: { value: "Yes, Wednesday, from 1 to 5." } });
    await tellClaude(EDIT_INSTRUCTION);
    await waitFor(() => {
      expect(writes.map((write) => (write.body as { draft_text?: unknown }).draft_text)).toEqual(["Yes, Wednesday, from 1 to 5."]);
    });
  });

  it("opens Details at what Claude now follows from See guidance", async () => {
    renderRequests(THREAD.henderson, { world: rewritten(requestsWorld(), THREAD.henderson) });
    const row = within(await conversation()).getByRole("article", { name: "Helen told Claude what to do differently" });
    fireEvent.click(within(row).getByRole("button", { name: "See guidance" }));
    const details = await screen.findByRole("complementary", { name: "Properties" });
    const guidance = within(details).getByRole("region", { name: "Guidance Claude follows" });
    await waitFor(() => {
      expect(document.activeElement).toBe(guidance);
    });
    expect(guidance.textContent).toContain(EDIT_INSTRUCTION);
    expect(guidance.textContent).toMatch(/Version 1 · from Dr\. Helen Ward's edit · /);
    expect(guidance.textContent).toContain("Claude reads this at its next turn");
  });

  it("keeps the draft readable but locked while Claude rewrites it: Approve waits, Decline and Tell Claude don't", async () => {
    renderRequests(THREAD.henderson, { steerHangs: true });
    const decision = await card();
    await tellClaude(EDIT_INSTRUCTION);
    expect(await within(decision).findByText("Claude is rewriting…")).toBeTruthy();
    expect(draft(decision).value).toBe(HENDERSON_ANSWER);
    expect(draft(decision).readOnly).toBe(true);
    expect(button(foot(decision), "Approve and send").disabled).toBe(true);
    expect(button(foot(decision), "Decline").disabled).toBe(false);
    expect(foot(decision).textContent).toContain("You can approve once the new draft is here");

    const region = await tellClaude("Also suggest Nov 10.", false);
    expect(await screen.findByText("Claude is still working on your edit.")).toBeTruthy();
    expect(button(region, "Tell Claude").disabled).toBe(false);
  });

  it("stops waiting for a rewrite that never comes after the fallback timeout", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] });
    renderRequests(THREAD.henderson, { steerHangs: true });
    const decision = await card();
    await tellClaude(EDIT_INSTRUCTION);
    expect(await within(decision).findByText("Claude is rewriting…")).toBeTruthy();
    expect(REDRAFT_TIMEOUT_MS).toBe(180_000);

    act(() => {
      vi.advanceTimersByTime(REDRAFT_TIMEOUT_MS);
    });
    expect(await within(decision).findByText("Draft reply · unchanged")).toBeTruthy();
    expect(button(foot(decision), "Approve and send").disabled).toBe(false);
  });

  it("reuses an Edit's client_key after no answer, and mints a new one after any answer", async () => {
    const dropped = renderRequests(THREAD.henderson, { first: { [steersRoute(THREAD.henderson)]: "drop" } });
    const region = await tellClaude(EDIT_INSTRUCTION);
    expect((await within(region.parentElement ?? region).findByRole("alert")).textContent).toBe(
      "Couldn't reach Harmony. Try again.",
    );
    expect(field(region).value).toBe(EDIT_INSTRUCTION);
    fireEvent.click(button(region, "Tell Claude"));
    await waitFor(() => {
      expect(keysOf(dropped.writes, steersRoute(THREAD.henderson))).toHaveLength(2);
    });
    const [lost, retried] = keysOf(dropped.writes, steersRoute(THREAD.henderson));
    expect(retried).toBe(lost);
    cleanup();

    const unavailable = new Response(null, { status: 503, statusText: "Service Unavailable" });
    const answered = renderRequests(THREAD.henderson, { first: { [steersRoute(THREAD.henderson)]: unavailable } });
    const again = await tellClaude(EDIT_INSTRUCTION);
    expect((await within(again.parentElement ?? again).findByRole("alert")).textContent).toBe(
      "Couldn't reach Claude. Try again.",
    );
    fireEvent.click(button(again, "Tell Claude"));
    await waitFor(() => {
      expect(keysOf(answered.writes, steersRoute(THREAD.henderson))).toHaveLength(2);
    });
    const [refused, fresh] = keysOf(answered.writes, steersRoute(THREAD.henderson));
    expect(fresh).not.toBe(refused);
  });

  const REFUSALS: readonly [string, Response, string][] = [
    [
      "steer_running",
      problem(409, "steer_running", "Claude is still working on an edit.", { by: raj }),
      "Claude is still working on Raj's edit. Try again once it lands.",
    ],
    ["thread_closed", problem(409, "thread_closed", "This conversation is closed."), "This conversation is closed. Reopen it to continue."],
    [
      "member_changed",
      problem(409, "member_changed", "You switched member.", { member: raj }),
      "You switched to Raj in another tab, so this wasn't sent.",
    ],
  ];

  it.each(REFUSALS)("keeps the instruction when an Edit is refused (%s)", async (_code, refusal, line) => {
    renderRequests(THREAD.henderson, { first: { [steersRoute(THREAD.henderson)]: refusal } });
    const region = await tellClaude(EDIT_INSTRUCTION);
    const alert = await within(region.parentElement ?? region).findByRole("alert");
    expect(alert.textContent).toContain(line);
    expect(field(region).value).toBe(EDIT_INSTRUCTION);
    expect(mode(region, "Edit").getAttribute("aria-checked")).toBe("true");
  });

  it("puts a failed Edit's text back in Edit, focused, on Try again", async () => {
    const world = edited(requestsWorld(), THREAD.henderson, {
      escalation_id: ESCALATION.henderson,
      draft_rev: 2,
      status: "failed",
      guidance_outcome: "failed",
      draft_outcome: "failed",
      failure_reason: "model_error",
    });
    renderRequests(THREAD.henderson, { world });
    const row = within(await conversation()).getByRole("article", { name: "Helen told Claude what to do differently" });
    expect(row.textContent).toContain("Couldn't reach Claude. Your instruction is saved; nothing changed.");
    fireEvent.click(within(row).getByRole("button", { name: "Try again" }));
    const region = await box();
    expect(mode(region, "Edit").getAttribute("aria-checked")).toBe("true");
    expect(field(region).value).toBe(EDIT_INSTRUCTION);
    expect(document.activeElement).toBe(field(region));
  });

  it("has no mentions in Edit, and suggests switching an @ to Comment", async () => {
    renderRequests(THREAD.henderson);
    const region = await tellClaude("@ra", false);
    expect(screen.queryByRole("listbox", { name: "Mention a teammate" })).toBeNull();
    expect(within(region).getByText("Mentions are for comments.")).toBeTruthy();
    fireEvent.click(button(region, "Switch to Comment?"));
    expect(mode(region, "Comment").getAttribute("aria-checked")).toBe("true");
    expect(field(region).value).toBe("@ra");
  });

  it("turns Edit off on a closed conversation, where Reopen picks it up", async () => {
    renderRequests(THREAD.ridgeline);
    const region = await box();
    const edit = mode(region, "Edit");
    expect(edit.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(edit);
    expect(mode(region, "Comment").getAttribute("aria-checked")).toBe("true");
    expect(field(region).getAttribute("aria-label")).toBe("Comment");
  });

  it("carries an unsent Edit into Reopen when the conversation closes under it", async () => {
    const world = requestsWorld();
    const { queryClient } = renderRequests(THREAD.pacific, { world });
    const region = await tellClaude(REOPEN_INSTRUCTION, false);
    reopenOffer(ended(world, THREAD.pacific, { end_reason: "quiet" }), THREAD.pacific, {
      state: "wrapping_up",
      block: null,
    });
    await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.thread(THREAD.pacific) }));

    // Claude never reads a comment, so the instruction mustn't stay where Enter posts one.
    const prompt = await screen.findByRole("region", { name: `Reopen with ${orgs.pacific.name}` });
    expect(within(prompt).getByRole<HTMLTextAreaElement>("textbox").value).toBe(REOPEN_INSTRUCTION);
    expect(mode(region, "Comment").getAttribute("aria-checked")).toBe("true");
    expect(field(region).value).toBe("");
    // It waits there, as the Closed row's Reopen would, until Claude has wrapped up.
    expect(button(prompt, "Reopen").disabled).toBe(true);
    expect(within(prompt).getByText("Claude is still wrapping up this conversation.")).toBeTruthy();
  });

  it("keeps what the member is typing, and the field itself, while an item opens and resolves elsewhere", async () => {
    const world = requestsWorld();
    const item = world.threads.get(THREAD.henderson)?.workspace.escalations[0];
    if (item === undefined) throw new Error("no item on Henderson");
    item.status = "resolved";
    const { sources } = await renderLive(THREAD.henderson, { world });
    const input = field(await box());
    input.focus();
    fireEvent.change(input, { target: { value: "Half a thought about the reserve" } });

    const changed = (seq: number) => {
      act(() => {
        sources.latest.send(
          durable(
            seq,
            {
              type: "entity.snapshot",
              entity_type: "escalation",
              entity_id: ESCALATION.henderson,
              version: seq,
              data: { thread_id: THREAD.henderson },
            },
            { ...worldScope, kind: "thread", org_id: orgs.northlake.id, thread_id: THREAD.henderson },
          ),
        );
      });
    };
    item.status = "open";
    changed(10_001);
    await card();
    expect(field(await box())).toBe(input);
    expect(input.value).toBe("Half a thought about the reserve");
    expect(document.activeElement).toBe(input);

    item.status = "resolved";
    changed(10_002);
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    expect(field(await box())).toBe(input);
    expect(input.value).toBe("Half a thought about the reserve");
  });

  it("highlights only the mentions a comment stored, so an @ that shared nothing reads as plain text", async () => {
    renderRequests(THREAD.calder);
    const region = await box();
    type(region, "@Raj, and @raj, see me@example.com");
    fireEvent.keyDown(field(region), { key: "Enter" });
    const list = await conversation();
    const comment = await within(list).findByRole("article", { name: "Comment from Dr. Helen Ward" });
    expect(within(comment).getAllByText(/@/).map((element) => element.textContent)).toEqual([
      "@Raj, and @raj, see me@example.com",
      "@raj",
    ]);
  });

  it("colours a teammate's mention in the field over a copy of exactly its text, and only then", async () => {
    renderRequests(THREAD.henderson);
    const region = await box();
    const input = field(region);
    const backdrop = () => input.previousElementSibling;

    type(region, "@raj can you hold Nov 4?\n");
    // The team's members load after the box.
    const copy = await waitFor(() => {
      const element = backdrop();
      expect(element).not.toBeNull();
      return element;
    });
    expect(copy?.getAttribute("aria-hidden")).toBe("true");
    // Its text is the field's, plus the space that holds the final empty line open.
    expect(copy?.textContent).toBe("@raj can you hold Nov 4?\n ");
    expect(copy?.querySelector(".text-link")?.textContent).toBe("@raj");
    expect(input.className).toContain("text-transparent");

    type(region, "Can you hold Nov 4? @nobody");
    expect(backdrop()).toBeNull();
    expect(input.className).toContain("text-ink");

    type(region, "@raj can you hold Nov 4?");
    fireEvent.click(mode(region, "Edit"));
    expect(backdrop()).toBeNull();
    expect(input.className).toContain("text-ink");
  });

  it("puts the caret after a picked mention at once, so the next keystroke follows it", async () => {
    renderRequests(THREAD.henderson);
    const input = field(await box());
    fireEvent.change(input, { target: { value: "@ra please check" } });
    input.setSelectionRange(3, 3);
    fireEvent.select(input);
    await screen.findByRole("listbox", { name: "Mention a teammate" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(input.value).toBe("@raj please check");
    expect(input.selectionStart).toBe("@raj ".length);
  });

  it("ties the mention list to the field for screen readers, and Escape closes only the list", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const input = field(await box());
    fireEvent.change(input, { target: { value: "Suggest Nov 10 instead, cc @" } });
    const list = await screen.findByRole("listbox", { name: "Mention a teammate" });
    const [option] = within(list).getAllByRole("option");
    expect(input.getAttribute("aria-autocomplete")).toBe("list");
    expect(input.getAttribute("aria-controls")).toBe(list.id);
    expect(input.getAttribute("aria-activedescendant")).toBe(option?.id);
    expect(screen.getAllByRole("status").map((status) => status.textContent)).toContain(
      "1 teammate to mention. Up and down to choose, Enter or Tab to insert, Escape to close.",
    );

    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("listbox", { name: "Mention a teammate" })).toBeNull();
    expect(input.value).toBe("Suggest Nov 10 instead, cc @");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => {
      expect(writes.map((write) => write.route)).toEqual([notesRoute(THREAD.henderson)]);
    });
  });
});

describe("while the budget stops live turns", () => {
  // Each of these hands the move to a model whose call would be refused; comments and
  // the draft's wording call none.
  const HOLDS = [
    ["paused", "Live models are paused"],
    ["demo_exhausted", "The demo's live-model budget is used up"],
  ] as const;

  it.each(HOLDS)("holds Approve, Decline and Tell Claude while %s, but not comments or wording", async (budget, line) => {
    const { writes } = await renderLive(THREAD.henderson, { budget });
    const decision = await card();
    // The strip says why, on the card's top edge.
    const strip = await screen.findByText(line);
    expect(strip.closest("[role='status']")?.nextElementSibling).toBe(decision);
    expect(button(foot(decision), "Approve and send").disabled).toBe(true);
    expect(button(foot(decision), "Decline").disabled).toBe(true);
    fireEvent.keyDown(draft(decision), { key: "Enter", metaKey: true });
    fireEvent.change(draft(decision), { target: { value: "Yes, Wednesday." } });
    expect(within(decision).getByText("· edited by you")).toBeTruthy();

    const region = await tellClaude(EDIT_INSTRUCTION, false);
    expect(button(region, "Tell Claude").disabled).toBe(true);
    expect(screen.getByText(`${line}. Tell Claude works again once it's back.`)).toBeTruthy();
    fireEvent.keyDown(field(region), { key: "Enter" });
    expect(writes).toEqual([]);

    fireEvent.click(mode(region, "Comment"));
    fireEvent.click(button(region, "Add comment"));
    await waitFor(() => {
      expect(writes).toEqual([
        { route: notesRoute(THREAD.henderson), body: { body: EDIT_INSTRUCTION, as_member_id: members.helen.id } },
      ]);
    });
  });

  it.each(HOLDS)("holds Reopen while %s", async (budget, line) => {
    await renderLive(THREAD.ridgeline, { budget });
    const row = await closedRow();
    await waitFor(() => {
      expect(button(row, "Reopen").disabled).toBe(true);
    });
    expect(within(row).getByText(line)).toBeTruthy();
  });
});

describe("a closed conversation", () => {
  it("ends with a Closed row: why, our own summary, and Reopen", async () => {
    renderRequests(THREAD.ridgeline);
    const row = await closedRow();
    expect(row.textContent).toMatch(/^Closed · .+ · Ridgeline Fabrication closed it/);
    expect(row.textContent).toContain("Ridgeline Fabrication booked CT on four machined holder tubes");
    expect(row.textContent).toContain("Claude for Northlake's summary. Only Northlake sees it.");
    expect(button(row, "Reopen")).toBeTruthy();
    expect(screen.getByRole("separator", { name: "Claude for Ridgeline Fabrication closed the conversation" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    expect(screen.getByText("Closed", { selector: "span[class*='rounded-full']" })).toBeTruthy();
  });

  type Ending = Parameters<typeof ended>[2];
  const ENDINGS: readonly [string, Ending, string][] = [
    ["they closed it", { end_reason: "closed", closed_by: "requester" }, "Pacific Beamline closed it"],
    ["we closed it", { end_reason: "closed", closed_by: "responder" }, "Claude closed it"],
    ["both sides went quiet", { end_reason: "quiet" }, "both sides went quiet"],
    ["nothing was sent", { end_reason: "unsent" }, "nothing was sent"],
    ["the turn limit", { end_reason: "turn_limit" }, "it reached its turn limit"],
    ["the deadline", { end_reason: "deadline" }, "it reached its deadline"],
    ["a model error", { end_reason: "stopped", stop_reason: "model_error" }, stoppedLine("model_error", "ok")],
    ["a refusal", { end_reason: "stopped", stop_reason: "refused" }, stoppedLine("refused", "ok")],
    ["a pause", { end_reason: "stopped", stop_reason: "paused" }, stoppedLine("paused", "ok")],
    ["a spent budget", { end_reason: "stopped", stop_reason: "demo_budget" }, stoppedLine("demo_budget", "ok")],
  ];

  it.each(ENDINGS)("says why the run ended when %s", async (_why, ending, line) => {
    renderRequests(THREAD.pacific, { world: ended(requestsWorld(), THREAD.pacific, ending) });
    const row = await closedRow();
    expect(row.textContent).toMatch(/^Closed · /);
    expect(row.textContent).toContain(` · ${line}`);
    expect(button(row, "Reopen")).toBeTruthy();
  });

  it("opens the Reopen prompt where the button was; Cancel and Esc put the button back", async () => {
    renderRequests(THREAD.ridgeline);
    fireEvent.click(button(await closedRow(), "Reopen"));
    const prompt = await screen.findByRole("region", { name: "Reopen with Ridgeline Fabrication" });
    const input = within(prompt).getByRole<HTMLTextAreaElement>("textbox", { name: "What should Claude pick up?" });
    expect(document.activeElement).toBe(input);
    expect(button(prompt, "Reopen").disabled).toBe(true);
    expect(prompt.textContent).toContain(
      "Claude writes the next message, following your usual review settings. Only Northlake sees what you typed.",
    );
    // The prompt took the button's place.
    expect(within(await closedRow()).getAllByRole("button", { name: "Reopen" })).toEqual([button(prompt, "Reopen")]);

    fireEvent.click(button(prompt, "Cancel"));
    expect(screen.queryByRole("region", { name: "Reopen with Ridgeline Fabrication" })).toBeNull();
    fireEvent.click(button(await closedRow(), "Reopen"));
    const again = await screen.findByRole("region", { name: "Reopen with Ridgeline Fabrication" });
    fireEvent.keyDown(within(again).getByRole("textbox"), { key: "Escape" });
    expect(screen.queryByRole("region", { name: "Reopen with Ridgeline Fabrication" })).toBeNull();
    expect(button(await closedRow(), "Reopen")).toBeTruthy();
  });

  it("holds Reopen back while Claude is still wrapping up, with no writing row", async () => {
    const { sources } = await renderLive(THREAD.ridgeline, {
      world: reopenOffer(requestsWorld(), THREAD.ridgeline, { state: "wrapping_up", block: null }),
    });
    const row = await closedRow();
    expect(button(row, "Reopen").disabled).toBe(true);
    expect(within(row).getByText("Claude is still wrapping up this conversation.")).toBeTruthy();

    // The last turn writes only our summary: nothing more is coming to Ridgeline.
    act(() => {
      sources.latest.send(
        ephemeral({
          type: "stream.delta",
          org_id: orgs.northlake.id,
          thread_id: THREAD.ridgeline,
          turn_id: "0b1d7a3e-0000-4000-8000-0000000009f4",
          attempt: 1,
          text: "Summary of the conversation",
        }),
      );
    });
    const list = await conversation();
    expect(within(list).queryByRole("article", { name: /is writing$/ })).toBeNull();
    expect(list.textContent).not.toContain("Summary of the conversation");
  });

  const BLOCKS: readonly ReopenBlock[] = ["refused", "limit", "they_closed", "unfinished", "not_asked"];

  it.each(BLOCKS)("says why it can't be reopened (%s), with no button", async (block) => {
    renderRequests(THREAD.ridgeline, { world: reopenOffer(requestsWorld(), THREAD.ridgeline, { state: "blocked", block }) });
    const row = await closedRow();
    const line = blockLine(block, shortOrgName(orgs.northlake.name), orgs.ridgeline.name);
    if (line === null) throw new Error(`no line for ${block}`);
    expect(within(row).getByText(line)).toBeTruthy();
    expect(within(row).queryByRole("button", { name: "Reopen" })).toBeNull();
  });

  it("shows no Reopen on a conversation from the seeded history", async () => {
    renderRequests(THREAD.survey);
    const row = await closedRow();
    expect(within(row).queryByRole("button")).toBeNull();
    expect(row.textContent).not.toMatch(/reopen/i);
  });

  it("reopens with what the member typed, as the member, and shows Claude picking it up", async () => {
    const { writes } = renderRequests(THREAD.ridgeline);
    const prompt = await openReopen(REOPEN_INSTRUCTION);
    fireEvent.keyDown(within(prompt).getByRole("textbox"), { key: "Enter" });

    const row = await within(await conversation()).findByRole("article", { name: "Helen is reopening the conversation" });
    expect(row.textContent).toContain(`“${REOPEN_INSTRUCTION}”`);
    expect(row.textContent).toContain("Claude is picking this up…");
    expect(within(await closedRow()).queryByRole("button", { name: "Reopen" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Reopen with Ridgeline Fabrication" })).toBeNull();
    expect(writes).toEqual([
      {
        route: reopenRoute(THREAD.ridgeline),
        body: { client_key: expect.any(String) as unknown, body: REOPEN_INSTRUCTION, as_member_id: members.helen.id },
      },
    ]);
  });

  it("shows the reopened run: the guidance, Claude's next message, Edit back on, and the first run's summary kept", async () => {
    renderRequests(THREAD.ridgeline, { reopenSettles: "opened" });
    const prompt = await openReopen(REOPEN_INSTRUCTION);
    fireEvent.click(button(prompt, "Reopen"));

    const list = await conversation();
    const row = await within(list).findByRole("article", { name: "Helen reopened the conversation" });
    expect(row.textContent).toContain("Claude updated its guidance and writes the next message.");
    expect(await within(list).findByText(/^Picking this back up/)).toBeTruthy();
    expect(mode(await box(), "Edit").getAttribute("aria-disabled")).toBeNull();
    const first = await closedRow();
    expect(first.textContent).toContain("Ridgeline Fabrication booked CT on four machined holder tubes");
    expect(within(first).queryByRole("button", { name: "Reopen" })).toBeNull();

    fireEvent.click(within(row).getByRole("button", { name: "See guidance" }));
    const guidance = within(await screen.findByRole("complementary", { name: "Properties" })).getByRole("region", {
      name: "Guidance Claude follows",
    });
    expect(guidance.textContent).toContain(REOPEN_INSTRUCTION);
  });

  const FAILURES: readonly NonNullable<SteerView["failure_reason"]>[] = [
    "unchanged",
    "added_details",
    "refused",
    "invalid",
    "model_error",
    "reopen_failed",
  ];

  it.each(FAILURES)("says a reopen that failed (%s) changed nothing, with Try again", async (reason) => {
    renderRequests(THREAD.ridgeline, { world: reopenFailed(requestsWorld(), THREAD.ridgeline, reason) });
    const row = await within(await conversation()).findByRole("article", { name: "Helen tried to reopen the conversation" });
    expect(row.textContent).toContain(reopenFailure(reason, "ok").text);
    expect(row.textContent).toContain("Nothing was sent, and Ridgeline Fabrication didn't see this. Your text is saved.");
    expect(within(await closedRow()).queryByRole("button", { name: "Reopen" })).toBeNull();

    fireEvent.click(within(row).getByRole("button", { name: "Try again" }));
    const prompt = await screen.findByRole("region", { name: "Reopen with Ridgeline Fabrication" });
    expect(within(prompt).getByRole<HTMLTextAreaElement>("textbox").value).toBe(REOPEN_INSTRUCTION);
  });

  it("keeps the reopen's client_key across no answer and a 409, and mints a new one after Cancel or a failed attempt", async () => {
    const closing = problem(409, "closing_in_progress", "Claude is still wrapping up.");
    const kept = renderRequests(THREAD.ridgeline, { first: { [reopenRoute(THREAD.ridgeline)]: "drop" } });
    let prompt = await openReopen(REOPEN_INSTRUCTION);
    fireEvent.click(button(prompt, "Reopen"));
    expect((await within(prompt).findByRole("alert")).textContent).toBe("Couldn't reach Harmony. Try again.");
    expect(within(prompt).getByRole<HTMLTextAreaElement>("textbox").value).toBe(REOPEN_INSTRUCTION);
    fireEvent.click(button(prompt, "Reopen"));
    await within(await conversation()).findByRole("article", { name: "Helen is reopening the conversation" });
    const [lost, retried] = keysOf(kept.writes, reopenRoute(THREAD.ridgeline));
    expect(retried).toBe(lost);
    cleanup();

    const refused = renderRequests(THREAD.ridgeline, { first: { [reopenRoute(THREAD.ridgeline)]: closing } });
    prompt = await openReopen(REOPEN_INSTRUCTION);
    fireEvent.click(button(prompt, "Reopen"));
    expect((await within(prompt).findByRole("alert")).textContent).toBe(
      "Claude is still wrapping up this conversation. Try again in a moment.",
    );
    fireEvent.click(button(prompt, "Cancel"));
    prompt = await openReopen(REOPEN_INSTRUCTION);
    fireEvent.click(button(prompt, "Reopen"));
    await within(await conversation()).findByRole("article", { name: "Helen is reopening the conversation" });
    const [before, after] = keysOf(refused.writes, reopenRoute(THREAD.ridgeline));
    expect(after).not.toBe(before);
    cleanup();

    const failing = renderRequests(THREAD.ridgeline, { reopenSettles: "unchanged" });
    prompt = await openReopen(REOPEN_INSTRUCTION);
    fireEvent.click(button(prompt, "Reopen"));
    const row = await within(await conversation()).findByRole("article", { name: "Helen tried to reopen the conversation" });
    fireEvent.click(within(row).getByRole("button", { name: "Try again" }));
    prompt = await screen.findByRole("region", { name: "Reopen with Ridgeline Fabrication" });
    fireEvent.click(button(prompt, "Reopen"));
    await waitFor(() => {
      expect(keysOf(failing.writes, reopenRoute(THREAD.ridgeline))).toHaveLength(2);
    });
    const [failed, next] = keysOf(failing.writes, reopenRoute(THREAD.ridgeline));
    expect(next).not.toBe(failed);
  });

  it("keeps the text when a reopen is refused for a switched member", async () => {
    const switched = problem(409, "member_changed", "You switched member.", { member: raj });
    renderRequests(THREAD.ridgeline, { first: { [reopenRoute(THREAD.ridgeline)]: switched } });
    const prompt = await openReopen(REOPEN_INSTRUCTION);
    fireEvent.click(button(prompt, "Reopen"));
    expect((await within(prompt).findByRole("alert")).textContent).toBe(
      "You switched to Raj in another tab, so this wasn't sent.",
    );
    expect(within(prompt).getByRole<HTMLTextAreaElement>("textbox").value).toBe(REOPEN_INSTRUCTION);
  });

  it("moves the text to Edit when they reopened it first", async () => {
    const world = requestsWorld();
    renderRequests(THREAD.ridgeline, {
      world,
      overrides: {
        "POST /api/threads/{thread_id}/reopen": () => {
          reopened(world, THREAD.ridgeline, "them");
          return problem(409, "thread_open", "This conversation is open.", {
            reopened_by: { org: orgs.ridgeline, member: null },
          });
        },
      },
    });
    const prompt = await openReopen(REOPEN_INSTRUCTION);
    fireEvent.click(button(prompt, "Reopen"));

    const region = await box();
    expect(await within(region).findByRole("status")).toHaveProperty(
      "textContent",
      "Ridgeline Fabrication reopened it first. Your text is in Edit, ready to send.",
    );
    await waitFor(() => {
      expect(mode(region, "Edit").getAttribute("aria-checked")).toBe("true");
    });
    expect(field(region).value).toBe(REOPEN_INSTRUCTION);
    expect(screen.queryByRole("region", { name: "Reopen with Ridgeline Fabrication" })).toBeNull();
    expect(
      await within(await conversation()).findByRole("article", { name: "Ridgeline Fabrication reopened the conversation" }),
    ).toBeTruthy();
  });

  it("shows their reopen with only their org, never what they typed", async () => {
    renderRequests(THREAD.ridgeline, { world: reopened(requestsWorld(), THREAD.ridgeline, "them") });
    const list = await conversation();
    const row = await within(list).findByRole("article", { name: "Ridgeline Fabrication reopened the conversation" });
    expect(row.textContent).toMatch(/^Ridgeline Fabrication reopened the conversation · /);
    expect(row.textContent).not.toContain("“");
    expect(within(list).getByText(/^We'd like to pick this back up/)).toBeTruthy();
    expect(screen.queryByText(REOPEN_INSTRUCTION, { exact: false })).toBeNull();
  });
});
