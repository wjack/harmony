// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import { queryKeys } from "../api/queryKeys";
import type { MemberView } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import type { BudgetState, BudgetView } from "../app/budget";
import { fetchStreamSnapshot } from "../app/snapshot";
import { failureLine, stoppedLine } from "../chat/failureCopy";
import { sharingPath } from "../customize/sections";
import { fixtureFetch, problem, type FixtureRoutes } from "../fixtures";
import {
  ESCALATION,
  HENDERSON_QUESTION,
  HENDERSON_SHORT_QUESTION,
  HENDERSON_WHY,
  THREAD,
  expired,
  hendersonDraft,
  requestsRoutes,
  requestsWorld,
  routedToBoth,
  routedToRaj,
  stopped,
  waitingOnThem,
  type RequestsRouteOptions,
  type StopReason,
} from "../fixtures/requests";
import { members, orgs } from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { durable, ephemeral, EventSources, worldScope } from "../realtime/testing";
import { toastStore } from "../shell/toasts";
import { TooltipProvider } from "../shell/ui";
import { RequestsPage } from "./RequestsPage";
import { REDRAFT_TIMEOUT_MS } from "./useDecision";

interface Call {
  route: string;
  body: unknown;
}

interface RenderOptions extends RequestsRouteOptions {
  as?: MemberView;
  /** Routes that replace the fixtures' own. */
  overrides?: FixtureRoutes;
  /** Start the event stream, so the session (and with it the budget) loads. */
  live?: boolean;
  /** Writes whose first call fails as an unavailable server would: a 503 with no body. */
  failFirst?: readonly string[];
}

/** `/requests/:threadId?` over the requests fixtures, recording every write. */
function renderRequests(
  threadId: string | null,
  { overrides, live = false, failFirst = [], ...options }: RenderOptions = {},
) {
  const serve = fixtureFetch({ ...requestsRoutes({ world: requestsWorld(), ...options }), ...overrides });
  const writes: Call[] = [];
  const failing = new Set(failFirst);
  const api = createApiClient({
    baseUrl: "http://harmony.test",
    fetch: async (request) => {
      if (request.method !== "GET") {
        const text = await request.clone().text();
        const route = `${request.method} ${new URL(request.url).pathname}`;
        writes.push({ route, body: text === "" ? undefined : (JSON.parse(text) as unknown) });
        if (failing.delete(route)) return new Response(null, { status: 503, statusText: "Service Unavailable" });
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
  const router = createMemoryRouter([{ path: "/requests/:threadId?", element: <RequestsPage /> }], {
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

const card = () => screen.findByRole("region", { name: "Needs your decision" });
/** The view's one composer, which acts on the open item. */
const composer = () => screen.findByRole("region", { name: "Your decision" });
const field = (region: HTMLElement) => within(region).getByRole<HTMLTextAreaElement>("textbox");
const resolveRoute = (escalationId: string) => `POST /api/escalations/${escalationId}/resolve`;
const redraftRoute = (escalationId: string) => `POST /api/escalations/${escalationId}/redraft`;
const notesRoute = (threadId: string) => `POST /api/threads/${threadId}/notes`;
const HENDERSON_ANSWER = hendersonDraft("").answer;

function type(region: HTMLElement, value: string) {
  fireEvent.change(field(region), { target: { value } });
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
    const henderson = within(needs).getByRole("link", { name: /XRD and SEM\/EDS quote/ });
    expect(henderson.getAttribute("href")).toBe(`/requests/${THREAD.henderson}`);
    expect(henderson.textContent).toMatch(/Henderson Lab · Needs your answer · /);
    // Helen hasn't opened it in this browser: the link's name says so, not just a dot.
    expect(within(needs).getByRole("link", { name: /^XRD and SEM\/EDS quote.*, unread$/ })).toBe(henderson);
    expect(within(needs).queryByLabelText("Unread")).toBeNull();
    expect(screen.getByRole("region", { name: /^Done/ })).toBeTruthy();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search requests" }), { target: { value: "peek" } });
    expect(screen.queryByRole("region", { name: /^Needs you/ })).toBeNull();
    const found = screen.getAllByRole("link");
    expect(found).toHaveLength(1);
    expect(found[0]?.textContent).toContain("SEM on machined PEEK holder surfaces");

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
});

describe("the view", () => {
  it("heads the thread with its title, the counterparty, a status pill and Details", async () => {
    renderRequests(THREAD.henderson);
    expect(await screen.findByRole("heading", { level: 1, name: "XRD and SEM/EDS quote" })).toBeTruthy();
    expect(screen.getByText("with Henderson Lab")).toBeTruthy();
    expect(screen.getByText("Needs your answer")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Details" }).getAttribute("aria-expanded")).toBe("false");
  });

  it("lets a status that names the counterparty stand for \"with\" them, whole in its title", async () => {
    renderRequests(THREAD.pacific, { world: waitingOnThem(requestsWorld(), THREAD.pacific) });
    await screen.findByRole("heading", { level: 1, name: "SEM cross-sections of coated foils" });

    const status = `Waiting on ${orgs.pacific.name}`;
    // A long status truncates before the title does; its title attribute keeps it whole.
    expect(screen.getByText(status).closest("[title]")?.getAttribute("title")).toBe(status);
    expect(screen.queryByText(`with ${orgs.pacific.name}`)).toBeNull();
  });

  it("keeps Details closed until asked, then holds Share, what our representative saw, Properties and Activity", async () => {
    renderRequests(THREAD.henderson);
    await card();
    expect(screen.queryByRole("complementary", { name: "Properties" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    const details = screen.getByRole("complementary", { name: "Properties" });
    expect(within(details).getByRole("button", { name: "Share" })).toBeTruthy();
    expect(within(details).getByRole("button", { name: /^What our representative saw/ })).toBeTruthy();
    expect(within(details).getByRole("heading", { name: "Properties" })).toBeTruthy();
    expect(within(details).getByRole("heading", { name: "Activity" })).toBeTruthy();
    expect(within(details).getByText("Henderson Lab")).toBeTruthy();
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

  it("gives focus back to Edit when Esc ends editing in the draft", async () => {
    renderRequests(THREAD.henderson);
    const decision = await card();
    const actions = await composer();
    fireEvent.click(within(actions).getByRole("button", { name: "Edit" }));
    const answer = within(decision).getByRole("textbox", { name: "Answer" });
    expect(document.activeElement).toBe(answer);

    fireEvent.keyDown(answer, { key: "Escape" });
    expect(within(decision).queryByRole("textbox", { name: "Answer" })).toBeNull();
    expect(document.activeElement).toBe(within(actions).getByRole("button", { name: "Edit" }));
  });

  it("shows our consult as one line, the whole question behind it", async () => {
    renderRequests(THREAD.henderson);
    const thread = await screen.findByRole("list", { name: "Thread" });
    const asked = within(thread).getByRole("button", { name: /^Our Claude asked the team/ });
    expect(asked.textContent).toMatch(/^Our Claude asked the team · only Northlake Characterization Core sees this · /);
    expect(asked.getAttribute("aria-expanded")).toBe("false");
    expect(thread.textContent).not.toContain(HENDERSON_QUESTION);
    fireEvent.click(asked);
    expect(within(thread).getByText(HENDERSON_QUESTION)).toBeTruthy();
  });

  it("shows who is writing as a row in the message style, never a bubble", async () => {
    const { sources } = renderRequests(THREAD.henderson, { live: true });
    await screen.findByRole("list", { name: "Thread" });
    await waitFor(() => {
      expect(sources.opened).toHaveLength(1);
    });
    const delta = (org: string, text: string) =>
      ephemeral({
        type: "stream.delta",
        org_id: org,
        thread_id: THREAD.henderson,
        turn_id: "0b1d7a3e-0000-4000-8000-0000000009f1",
        attempt: 1,
        text,
      });

    act(() => {
      sources.latest.accept();
      sources.latest.send(delta(orgs.northlake.id, "Checking the XRD calendar"));
    });
    const ours = await screen.findByRole("article", { name: "Claude for Northlake Characterization Core is writing" });
    const text = within(ours).getByText("Checking the XRD calendar");
    expect(text.className).toContain("font-serif");
    expect(ours.querySelector("[class*='bg-bubble'], [class*='rounded-[']")).toBeNull();
    expect(text.className).not.toMatch(/\b(bg-|border|rounded)/);

    // Each delta grows the text; only the fixed line is announced, never the text again.
    act(() => {
      sources.latest.send(delta(orgs.northlake.id, " for Thursday"));
    });
    expect(within(ours).getByText("Checking the XRD calendar for Thursday")).toBeTruthy();
    const announced = screen.getAllByRole("status").map((status) => status.textContent);
    expect(announced).toContain("Claude for Northlake Characterization Core is writing…");
    expect(announced.join(" ")).not.toContain("XRD calendar");
    expect(text.closest("[role='status'], [aria-live]")).toBeNull();
  });

  it("shows a closed thread with our own summary", async () => {
    renderRequests(THREAD.ridgeline);
    const ended = await screen.findByRole("region", { name: "Closed" });
    expect(ended.textContent).toContain("Ridgeline Fabrication booked SEM on four machined PEEK holders");
    expect(ended.textContent).toContain("Only Northlake Characterization Core sees it.");
    expect(screen.getByRole("separator", { name: "Claude for Ridgeline Fabrication closed the thread" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    // Nothing open: the composer only adds notes.
    expect(screen.getByRole("region", { name: "Add a note" })).toBeTruthy();
    expect(screen.getByText("Only Northlake Characterization Core sees notes")).toBeTruthy();
  });

  const STOPS: readonly [StopReason, BudgetState][] = [
    ["session_budget", "session_exhausted"],
    ["daily_budget", "daily_exhausted"],
    ["demo_budget", "demo_exhausted"],
    ["demo_budget", "ok"],
    ["paused", "paused"],
    ["model_error", "ok"],
    ["refused", "ok"],
  ];

  it.each(STOPS)("says why the system stopped a thread (%s, budget %s)", async (reason, budget) => {
    renderRequests(THREAD.pacific, { world: stopped(requestsWorld(), THREAD.pacific, reason), budget, live: true });
    const banner = await screen.findByRole("region", { name: "Stopped" });
    await waitFor(() => {
      expect(banner.textContent).toBe(`Stopped${stoppedLine(reason, budget)}`);
    });
    expect(screen.queryByRole("region", { name: "Closed" })).toBeNull();
    expect(screen.getAllByText("Stopped").length).toBeGreaterThan(1);
  });

  it("shows our summary under why it stopped when the other side refused", async () => {
    const summary = "Pacific declined to continue; no quote. Ask Ridgeline next.";
    renderRequests(THREAD.pacific, { world: stopped(requestsWorld(), THREAD.pacific, "refused", summary) });
    const banner = await screen.findByRole("region", { name: "Stopped" });
    await waitFor(() => {
      expect(banner.textContent).toContain(summary);
    });
    expect(banner.textContent).toContain(stoppedLine("refused", "ok"));
    expect(banner.textContent).toContain("Only Northlake Characterization Core sees it.");
  });

  it("says an expired thread ended without a close, for every way it can expire", async () => {
    renderRequests(THREAD.pacific, { world: expired(requestsWorld(), THREAD.pacific) });
    const banner = await screen.findByRole("region", { name: "Expired" });
    expect(banner.textContent).toBe(
      "ExpiredIt ended without a close: both sides went quiet, or it reached its turn limit or deadline.",
    );
  });

  it("opens what our representative saw from Details", async () => {
    renderRequests(THREAD.henderson);
    await card();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    fireEvent.click(screen.getByRole("button", { name: /^What our representative saw/ }));
    const drawer = await screen.findByRole("dialog", { name: "What our representative saw" });
    const prompt = await within(drawer).findByRole("region", { name: "Its prompt" });
    expect(prompt.textContent).toContain("What it may pass on");
    // Raj's note never reached it: nothing has been released on this thread yet.
    expect(drawer.textContent).not.toContain("reserve XRD block Wed");
  });
});

describe("the decision card", () => {
  it("reads in full after the transcript: whose it is, the short question, why, the draft and what Claude looked at", async () => {
    renderRequests(THREAD.henderson);
    const decision = await card();
    expect(within(decision).getByText("Claude needs your answer before replying to Henderson Lab")).toBeTruthy();
    expect(within(decision).getByRole("heading", { level: 2 }).textContent).toBe(HENDERSON_SHORT_QUESTION);

    const why = within(decision).getByRole("region", { name: "Why it came to you" });
    expect(why.textContent).toContain("Northlake shares what's open on the public calendar, not internal reserve slots.");
    expect(why.textContent).toContain("(the sharing check)");
    expect(within(why).getByText(`Routed to you: ${HENDERSON_WHY.helen}`)).toBeTruthy();
    expect(within(why).getByRole("button", { name: "Reassign" })).toBeTruthy();

    expect(within(decision).getByRole("heading", { name: "Draft reply" })).toBeTruthy();
    expect(within(decision).getByText(/^Yes, we can fit them in\./)).toBeTruthy();
    const lookedAt = within(decision).getByRole("region", { name: "What Claude looked at" });
    const line = within(lookedAt).getByRole("button", { name: /^Looked at: / });
    expect(line.textContent).toBe(
      "Looked at: XRD availability before Nov 6 · Public calendar · Raj's note, reserve XRD block · External academic price list",
    );
    expect(within(lookedAt).queryAllByRole("listitem")).toHaveLength(0);
    fireEvent.click(line);
    expect(within(lookedAt).getAllByRole("listitem")).toHaveLength(4);
    expect(within(decision).getByRole("button", { name: "Claude's reasoning" })).toBeTruthy();
    const link = within(decision).getByRole("link", { name: "Edit what you share with Henderson Lab" });
    expect(link.getAttribute("href")).toBe(sharingPath(orgs.henderson.id));

    const thread = screen.getByRole("list", { name: "Thread" });
    expect(thread.compareDocumentPosition(decision) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(decision.contains(await composer())).toBe(false);
  });

  it("never scrolls inside itself, in view or in edit mode", async () => {
    // jsdom has no layout. The answer's text is 312px tall and the field has a 1px border:
    // its style height is the border box, as in the browser, and "auto" fits the text.
    const BORDERS = 2;
    const isAnswer = (element: Element) =>
      element instanceof HTMLTextAreaElement && element.getAttribute("aria-label") === "Answer";
    const borderBox = (element: HTMLElement) =>
      element.style.height === "auto" ? 312 + BORDERS : Number.parseFloat(element.style.height) || 0;
    vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(function (this: Element) {
      return isAnswer(this) ? 312 : 0;
    });
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
      return isAnswer(this) ? borderBox(this) : 0;
    });
    vi.spyOn(Element.prototype, "clientHeight", "get").mockImplementation(function (this: Element) {
      if (!(this instanceof HTMLElement)) return 0;
      return isAnswer(this) ? borderBox(this) - BORDERS : Number.parseFloat(this.style.height) || 0;
    });
    renderRequests(THREAD.henderson);
    const decision = await card();
    const scrolls = () => decision.querySelector("[class*='overflow-y-auto'], [class*='max-h-']");
    expect(decision.className).not.toMatch(/overflow-y-auto|max-h-/);
    expect(scrolls()).toBeNull();

    fireEvent.click(within(await composer()).getByRole("button", { name: "Edit" }));
    const answer = within(decision).getByRole<HTMLTextAreaElement>("textbox", { name: "Answer" });
    expect(answer.value).toBe(HENDERSON_ANSWER);
    expect(scrolls()).toBeNull();
    expect(answer.clientHeight).toBeGreaterThanOrEqual(answer.scrollHeight);
    expect(answer.scrollHeight).toBe(312);
  });

  it("titles the item with the representative's whole question when the answerer gave no short one", async () => {
    const world = requestsWorld();
    const item = world.threads.get(THREAD.henderson)?.workspace.escalations[0];
    if (item === undefined) throw new Error("no Henderson item");
    item.drafts = item.drafts.map((draft) => ({ ...draft, short_question: null }));
    renderRequests(THREAD.henderson, { world });
    expect(within(await card()).getByRole("heading", { level: 2 }).textContent).toBe(HENDERSON_QUESTION);
  });

  it("gives each assignee their own why, and says who else has it", async () => {
    renderRequests(THREAD.henderson, { as: members.raj, world: routedToBoth(requestsWorld()) });
    const why = within(await card()).getByRole("region", { name: "Why it came to you" });
    expect(within(why).getByText(`Routed to you: ${HENDERSON_WHY.raj}`)).toBeTruthy();
    expect(within(why).getByText("with Helen")).toBeTruthy();
    expect(screen.getByText("Claude needs your answer before replying to Henderson Lab")).toBeTruthy();
  });

  it("names the assignee to a teammate who isn't one, who may still act", async () => {
    renderRequests(THREAD.henderson, { as: members.raj });
    const decision = await card();
    expect(within(decision).getByText("Claude needs Helen's answer before replying to Henderson Lab")).toBeTruthy();
    // Helen's why speaks to her, so Raj reads it quoted, under her name.
    const why = within(decision).getByRole("region", { name: "Why it came to Helen" });
    expect(within(why).getByText(`Routed to Helen: “${HENDERSON_WHY.helen}”`)).toBeTruthy();
    expect(screen.getByText("Approving answers for Helen · sends this reply to Henderson Lab only")).toBeTruthy();
    expect(screen.getByText("Needs Helen")).toBeTruthy();
    expect(within(await composer()).getByRole("button", { name: "Approve and send" })).toBeTruthy();
  });

  it("reassigns from Why it came to you, then names the new assignee", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const decision = await card();
    expect(screen.getByText("Approving sends this reply to Henderson Lab only · ⌘↵ to approve")).toBeTruthy();
    const why = within(decision).getByRole("region", { name: "Why it came to you" });
    fireEvent.click(within(why).getByRole("button", { name: "Reassign" }));
    const picker = await screen.findByRole("listbox", { name: "Reassign to" });
    const raj = await within(picker).findByRole<HTMLButtonElement>("option", { name: /Raj Patel/ });
    // The assignee is listed, marked and not pickable.
    const helen = within(picker).getByRole<HTMLButtonElement>("option", { name: /Helen Ward/ });
    expect(helen.getAttribute("aria-selected")).toBe("true");
    expect(helen.disabled).toBe(true);
    expect(raj.disabled).toBe(false);
    fireEvent.click(raj);
    await waitFor(() => {
      expect(writes).toEqual([
        { route: `POST /api/escalations/${ESCALATION.henderson}/reassign`, body: { member_id: members.raj.id } },
      ]);
    });
    expect(await within(decision).findByText("Claude needs Raj's answer before replying to Henderson Lab")).toBeTruthy();
    expect(within(decision).getByText("Assigned to Raj")).toBeTruthy();
    expect(screen.getByText("Approving answers for Raj · sends this reply to Henderson Lab only")).toBeTruthy();
  });

  it("takes a member who reassigns away their only way in back to Requests", async () => {
    toastStore.setState({ toasts: [] });
    const { writes, router } = renderRequests(THREAD.henderson, {
      as: members.raj,
      world: routedToRaj(requestsWorld()),
    });
    const why = within(await card()).getByRole("region", { name: "Why it came to you" });
    fireEvent.click(within(why).getByRole("button", { name: "Reassign" }));
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
    expect(screen.queryByText("This thread isn't visible to you")).toBeNull();
  });

  it("says a request someone else took away is no longer visible, and keeps focus in the page", async () => {
    const world = routedToRaj(requestsWorld());
    const { router, queryClient } = renderRequests(THREAD.henderson, { as: members.raj, world });
    await card();
    const record = world.threads.get(THREAD.henderson);
    if (record !== undefined) record.detail.access = record.detail.access.filter((row) => row.member_id !== members.raj.id);
    await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.thread(THREAD.henderson) }));

    const back = await screen.findByRole("button", { name: "Back to Requests" });
    expect(screen.getByText("This request is no longer visible to you")).toBeTruthy();
    expect(screen.queryByText("This thread isn't visible to you")).toBeNull();
    expect(document.activeElement).toBe(back);
    fireEvent.click(back);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/requests");
    });
  });

  it("asks whether to send a held reply, whose reason isn't repeated as reasoning", async () => {
    const { writes } = renderRequests(THREAD.vantage);
    const decision = await card();
    expect(within(decision).getByText("Claude wants to send this to Vantage Market Intelligence")).toBeTruthy();
    expect(within(decision).getByRole("heading", { level: 2 }).textContent).toBe(
      "Send this reply to Vantage Market Intelligence?",
    );
    expect(within(decision).getByRole("region", { name: "Why it came to you" }).textContent).toContain(
      "A teammate approves every message in Ask mode.",
    );
    expect(within(decision).queryByRole("button", { name: "Claude's reasoning" })).toBeNull();

    fireEvent.click(within(await composer()).getByRole("button", { name: "Approve and send" }));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    expect(writes.map((write) => write.route)).toEqual([resolveRoute(ESCALATION.vantage)]);
    const thread = await screen.findByRole("list", { name: "Thread" });
    expect(await within(thread).findByText(/^We don't share who books our instruments/)).toBeTruthy();
  });
});

describe("the composer", () => {
  it("is the view's one textbox", async () => {
    renderRequests(THREAD.henderson);
    const actions = await composer();
    expect(within(actions).getAllByRole("textbox")).toHaveLength(1);
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(field(actions).placeholder).toBe("Tell Claude what to change…");
    expect(within(actions).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Decline",
      "Edit",
      "Approve and send",
    ]);
  });

  it("approves the draft as shown, and the member's edit", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const decision = await card();
    const actions = await composer();
    fireEvent.click(within(actions).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(decision).getByRole("textbox", { name: "Answer" }), { target: { value: "Yes, Wednesday." } });
    expect(screen.getByText(/^Edited · /)).toBeTruthy();
    fireEvent.click(within(actions).getByRole("button", { name: "Approve and send" }));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    expect(writes).toEqual([
      { route: resolveRoute(ESCALATION.henderson), body: { action: "approve", text: "Yes, Wednesday.", draft_rev: 1 } },
    ]);
  });

  it("approves the shown draft on ⌘↵ with nothing typed", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const actions = await composer();
    fireEvent.keyDown(field(actions), { key: "Enter", metaKey: true });
    await waitFor(() => {
      expect(writes).toEqual([
        { route: resolveRoute(ESCALATION.henderson), body: { action: "approve", text: HENDERSON_ANSWER, draft_rev: 1 } },
      ]);
    });
  });

  it("stores a consult's instruction as a note, then asks Claude to redraft with it", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const decision = await card();
    const actions = await composer();
    type(actions, "Shorter, please.");
    expect(within(actions).queryByRole("button", { name: "Approve and send" })).toBeNull();
    expect(within(actions).getByRole("button", { name: "Add note" })).toBeTruthy();
    fireEvent.click(within(actions).getByRole("button", { name: "Redraft" }));

    expect(await within(decision).findByText(/^Yes\. We can run XRD on Wednesday, Nov 4/)).toBeTruthy();
    expect(within(decision).getByText("2 of 2")).toBeTruthy();
    expect(writes).toEqual([
      { route: notesRoute(THREAD.henderson), body: { body: "Shorter, please." } },
      { route: redraftRoute(ESCALATION.henderson), body: { instruction: "Shorter, please." } },
    ]);
    const thread = screen.getByRole("list", { name: "Thread" });
    const notes = within(thread).getAllByRole("article", { name: "Internal note from Dr. Helen Ward" });
    expect(notes.at(-1)?.textContent).toContain("Shorter, please.");
    expect(field(actions).value).toBe("");
  });

  it("stores a held message's instruction as a note, then sends it back to Claude", async () => {
    const { writes } = renderRequests(THREAD.vantage);
    await card();
    const actions = await composer();
    type(actions, "Quote the public rate card instead.");
    fireEvent.click(within(actions).getByRole("button", { name: "Send back to Claude" }));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    expect(writes).toEqual([
      { route: notesRoute(THREAD.vantage), body: { body: "Quote the public rate card instead." } },
      {
        route: resolveRoute(ESCALATION.vantage),
        body: { action: "revise", text: "Quote the public rate card instead.", draft_rev: 1 },
      },
    ]);
  });

  it("only adds a note when the text mentions a teammate", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const actions = await composer();
    type(actions, "@raj can you confirm the reserve block?");
    // A mention is a member's slug, so it counts once the members load.
    await waitFor(() => {
      expect(within(actions).queryByRole("button", { name: "Redraft" })).toBeNull();
    });
    fireEvent.keyDown(field(actions), { key: "Enter" });
    await waitFor(() => {
      expect(writes).toEqual([
        { route: notesRoute(THREAD.henderson), body: { body: "@raj can you confirm the reserve block?" } },
      ]);
    });
    expect(await card()).toBeTruthy();
  });

  it("takes a mention as the server does: only a member's slug, wherever the server finds one", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const actions = await composer();
    // After a parenthesis it is a mention (once the members load), and sharing must not
    // also send a redraft.
    type(actions, "Loop in Raj (@raj) on the price");
    await waitFor(() => {
      expect(within(actions).queryByRole("button", { name: "Redraft" })).toBeNull();
    });
    // Not a member's slug, so they share nothing: the text can still go to Claude.
    type(actions, "Quote it @85 per sample instead");
    expect(within(actions).getByRole("button", { name: "Redraft" })).toBeTruthy();
    type(actions, "@Raj can you confirm the reserve block?");
    expect(within(actions).getByRole("button", { name: "Redraft" })).toBeTruthy();

    type(actions, "Loop in Raj (@raj) on the price");
    fireEvent.keyDown(field(actions), { key: "Enter" });
    await waitFor(() => {
      expect(writes).toEqual([{ route: notesRoute(THREAD.henderson), body: { body: "Loop in Raj (@raj) on the price" } }]);
    });
  });

  it("highlights only the mentions a note stored, so an @ that shared nothing reads as plain text", async () => {
    renderRequests(THREAD.calder);
    const notes = await screen.findByRole("region", { name: "Add a note" });
    const box = within(notes).getByRole<HTMLTextAreaElement>("textbox", { name: "Internal note" });
    fireEvent.change(box, { target: { value: "@Raj, and @raj, see me@example.com" } });
    fireEvent.keyDown(box, { key: "Enter" });
    const thread = screen.getByRole("list", { name: "Thread" });
    await waitFor(() => {
      expect(within(thread).getAllByRole("article", { name: "Internal note from Dr. Helen Ward" })).toHaveLength(1);
    });
    const note = within(thread).getByRole("article", { name: "Internal note from Dr. Helen Ward" });
    // The body, and in it one highlight: the mention that shared the thread.
    expect(within(note).getAllByText(/@/).map((element) => element.textContent)).toEqual([
      "@Raj, and @raj, see me@example.com",
      "@raj",
    ]);
  });

  it("puts the caret after a picked mention at once, so the next keystroke follows it", async () => {
    renderRequests(THREAD.henderson);
    const box = field(await composer());
    fireEvent.change(box, { target: { value: "@ra please check" } });
    box.setSelectionRange(3, 3);
    fireEvent.select(box);
    await screen.findByRole("listbox", { name: "Mention a teammate" });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(box.value).toBe("@raj please check");
    expect(box.selectionStart).toBe("@raj ".length);
  });

  it("ties the mention list to the field for screen readers, and Escape closes only the list", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const box = field(await composer());
    fireEvent.change(box, { target: { value: "Suggest Nov 10 instead, cc @" } });
    const list = await screen.findByRole("listbox", { name: "Mention a teammate" });
    const [raj] = within(list).getAllByRole("option");
    expect(box.getAttribute("aria-autocomplete")).toBe("list");
    expect(box.getAttribute("aria-controls")).toBe(list.id);
    expect(raj?.id).not.toBe("");
    expect(box.getAttribute("aria-activedescendant")).toBe(raj?.id);
    const statuses = screen.getAllByRole("status").map((status) => status.textContent);
    expect(statuses).toContain("1 teammate to mention. Up and down to choose, Enter or Tab to insert, Escape to close.");

    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByRole("listbox", { name: "Mention a teammate" })).toBeNull();
    expect(box.getAttribute("aria-activedescendant")).toBeNull();
    expect(box.value).toBe("Suggest Nov 10 instead, cc @");
    // With the list closed, Enter acts on the text again: a note, then the redraft.
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => {
      expect(writes.map((write) => write.route)).toEqual([
        notesRoute(THREAD.henderson),
        redraftRoute(ESCALATION.henderson),
      ]);
    });
  });

  it("declines in a step of its own: the note is set aside, and Cancel brings it back", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const actions = await composer();
    type(actions, "Check with Raj first.");
    fireEvent.click(within(actions).getByRole("button", { name: "Decline" }));
    const reason = within(actions).getByRole<HTMLTextAreaElement>("textbox", { name: "Reason for declining" });
    expect(reason.value).toBe("");
    expect(reason.placeholder).toBe("The reason Claude can give Henderson Lab (optional)…");
    expect(screen.getByText("Claude may pass this reason on to Henderson Lab · Esc to cancel")).toBeTruthy();
    fireEvent.click(within(actions).getByRole("button", { name: "Cancel" }));
    expect(within(actions).getByRole<HTMLTextAreaElement>("textbox", { name: "Instruction or note" }).value).toBe(
      "Check with Raj first.",
    );

    fireEvent.click(within(actions).getByRole("button", { name: "Decline" }));
    type(actions, "The reserve block is for internal work.");
    fireEvent.click(within(actions).getByRole("button", { name: "Decline" }));
    await waitFor(() => {
      expect(writes).toEqual([
        {
          route: resolveRoute(ESCALATION.henderson),
          body: { action: "decline", text: "The reserve block is for internal work.", draft_rev: 1 },
        },
      ]);
    });
  });

  it("keeps what the member is typing, and the field itself, while an item opens and resolves elsewhere", async () => {
    const world = requestsWorld();
    const item = world.threads.get(THREAD.henderson)?.workspace.escalations[0];
    if (item === undefined) throw new Error("no item on Henderson");
    item.status = "resolved";
    const { sources } = renderRequests(THREAD.henderson, { world, live: true });
    const box = field(await screen.findByRole("region", { name: "Add a note" }));
    await waitFor(() => {
      expect(sources.opened).toHaveLength(1);
    });
    act(() => {
      sources.latest.accept();
    });
    box.focus();
    fireEvent.change(box, { target: { value: "Half a thought about the reserve" } });

    // Claude consults the team, then a teammate answers in another tab.
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
    expect(field(await composer())).toBe(box);
    expect(box.value).toBe("Half a thought about the reserve");
    expect(document.activeElement).toBe(box);

    item.status = "resolved";
    changed(10_002);
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    expect(field(await screen.findByRole("region", { name: "Add a note" }))).toBe(box);
    expect(box.value).toBe("Half a thought about the reserve");
  });

  it("brings the set-aside note back, focused, once the decline goes through", async () => {
    renderRequests(THREAD.henderson);
    const actions = await composer();
    type(actions, "Check with Raj first.");
    fireEvent.click(within(actions).getByRole("button", { name: "Decline" }));
    type(actions, "The reserve block is for internal work.");
    const confirm = within(actions).getByRole("button", { name: "Decline" });
    confirm.focus();
    fireEvent.click(confirm);
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    const box = within(await screen.findByRole("region", { name: "Add a note" })).getByRole<HTMLTextAreaElement>(
      "textbox",
      { name: "Internal note" },
    );
    await waitFor(() => {
      expect(box.value).toBe("Check with Raj first.");
    });
    expect(document.activeElement).toBe(box);
  });

  it("keeps focus in the field when the pressed button goes with its state", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const actions = await composer();
    const press = (name: string) => {
      const button = within(actions).getByRole("button", { name });
      button.focus();
      fireEvent.click(button);
    };

    type(actions, "Check with Raj first.");
    press("Decline");
    press("Cancel");
    expect(document.activeElement).toBe(field(actions));
    expect(field(actions).value).toBe("Check with Raj first.");

    press("Add note");
    await waitFor(() => {
      expect(writes).toEqual([{ route: notesRoute(THREAD.henderson), body: { body: "Check with Raj first." } }]);
    });
    await waitFor(() => {
      expect(field(actions).value).toBe("");
    });
    expect(document.activeElement).toBe(field(actions));
  });

  it("brings focus back to the field once Approve and send goes with the item it approved", async () => {
    const { writes } = renderRequests(THREAD.henderson);
    const approve = within(await composer()).getByRole("button", { name: "Approve and send" });
    approve.focus();
    fireEvent.click(approve);
    await waitFor(() => {
      expect(writes.map((write) => write.route)).toEqual([resolveRoute(ESCALATION.henderson)]);
    });
    const notes = await screen.findByRole("region", { name: "Add a note" });
    expect(approve.isConnected).toBe(false);
    await waitFor(() => {
      expect(document.activeElement).toBe(field(notes));
    });
  });

  it("shows what a declined question and a declined reply told the representative", async () => {
    const declineWith = async (reason: string) => {
      const actions = await composer();
      fireEvent.click(within(actions).getByRole("button", { name: "Decline" }));
      type(actions, reason);
      fireEvent.keyDown(field(actions), { key: "Enter" });
      await waitFor(() => {
        expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
      });
    };
    const released = async () => {
      fireEvent.click(screen.getByRole("button", { name: "Details" }));
      fireEvent.click(screen.getByRole("button", { name: /^What our representative saw/ }));
      const drawer = await screen.findByRole("dialog", { name: "What our representative saw" });
      return within(drawer).findByRole("region", { name: "Released answers" });
    };

    renderRequests(THREAD.henderson);
    await declineWith("The reserve block is for internal work.");
    const answers = await released();
    expect(answers.textContent).toContain("Your side declined to answer. What you may say:");
    expect(answers.textContent).toContain("The reserve block is for internal work.");
    cleanup();
    window.localStorage.clear();

    renderRequests(THREAD.vantage);
    await declineWith("Point them to the public price list.");
    const told = await released();
    expect(within(told).getByRole("heading", { name: "About a message your side held" })).toBeTruthy();
    expect(told.textContent).toContain("Not sent: your side didn't approve it.");
  });

  it("refits an empty field to the placeholder each state shows", async () => {
    // jsdom has no layout: a placeholder over 40 characters wraps to two lines here.
    vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(function (this: Element) {
      if (!(this instanceof HTMLTextAreaElement)) return 0;
      return (this.value === "" ? this.placeholder : this.value).length > 40 ? 64 : 40;
    });
    renderRequests(THREAD.henderson);
    const actions = await composer();
    expect(field(actions).style.height).toBe("40px");

    fireEvent.click(within(actions).getByRole("button", { name: "Decline" }));
    expect(field(actions).placeholder).toBe("The reason Claude can give Henderson Lab (optional)…");
    expect(field(actions).style.height).toBe("64px");
    fireEvent.click(within(actions).getByRole("button", { name: "Cancel" }));
    expect(field(actions).style.height).toBe("40px");

    fireEvent.click(within(actions).getByRole("button", { name: "Approve and send" }));
    const notes = await screen.findByRole("region", { name: "Add a note" });
    expect(field(notes).placeholder).toBe("Add an internal note… Use @ to bring in a teammate");
    expect(field(notes).style.height).toBe("64px");
  });

  it("adds a note when nothing is open", async () => {
    const { writes } = renderRequests(THREAD.calder);
    const notes = await screen.findByRole("region", { name: "Add a note" });
    const box = within(notes).getByRole<HTMLTextAreaElement>("textbox", { name: "Internal note" });
    expect(box.placeholder).toBe("Add an internal note… Use @ to bring in a teammate");
    expect(within(notes).queryAllByRole("button")).toHaveLength(0);
    fireEvent.change(box, { target: { value: "Their budget is fixed this quarter." } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => {
      expect(writes).toEqual([{ route: notesRoute(THREAD.calder), body: { body: "Their budget is fixed this quarter." } }]);
    });
    expect(await screen.findByText("Their budget is fixed this quarter.")).toBeTruthy();
  });
});

describe("while the budget stops live turns", () => {
  // Each of these hands the move to a model whose call would be refused, ending the
  // thread for good (3.4); notes and editing call none.
  const HOLDS = [
    ["paused", "Live models are paused"],
    ["session_exhausted", "Session budget used up"],
  ] as const;

  /** The view at `threadId` once the session, and with it the budget, has loaded. */
  const renderHeld = async (threadId: string, budget: BudgetState, line: string) => {
    const rendered = renderRequests(threadId, { budget, live: true });
    await waitFor(() => {
      expect(rendered.sources.opened).toHaveLength(1);
    });
    act(() => {
      rendered.sources.latest.accept();
    });
    expect(await screen.findByText(line)).toBeTruthy();
    return rendered;
  };

  it.each(HOLDS)("holds a consult's approve, decline and redraft while %s, but not notes or editing", async (budget, line) => {
    const { writes } = await renderHeld(THREAD.henderson, budget, line);
    const decision = await card();
    const actions = await composer();
    const button = (name: string) => within(actions).getByRole<HTMLButtonElement>("button", { name });
    expect(button("Approve and send").disabled).toBe(true);
    expect(button("Decline").disabled).toBe(true);
    expect(button("Edit").disabled).toBe(false);
    fireEvent.keyDown(field(actions), { key: "Enter", metaKey: true });
    fireEvent.keyDown(decision, { key: "Enter", metaKey: true });

    type(actions, "Shorter, please.");
    expect(button("Redraft").disabled).toBe(true);
    fireEvent.keyDown(field(actions), { key: "Enter" });
    expect(field(actions).value).toBe("Shorter, please.");
    expect(writes).toEqual([]);

    fireEvent.click(button("Add note"));
    await waitFor(() => {
      expect(writes).toEqual([{ route: notesRoute(THREAD.henderson), body: { body: "Shorter, please." } }]);
    });
  });

  it.each(HOLDS)("holds a held message's approve and send-back while %s", async (budget, line) => {
    const { writes } = await renderHeld(THREAD.vantage, budget, line);
    const actions = await composer();
    expect(within(actions).getByRole<HTMLButtonElement>("button", { name: "Approve and send" }).disabled).toBe(true);
    type(actions, "Quote the public rate card instead.");
    expect(within(actions).getByRole<HTMLButtonElement>("button", { name: "Send back to Claude" }).disabled).toBe(true);
    fireEvent.keyDown(field(actions), { key: "Enter" });
    expect(field(actions).value).toBe("Quote the public rate card instead.");
    expect(writes).toEqual([]);
  });
});

describe("when deciding goes wrong", () => {
  it("keeps the draft readable but unapprovable while Claude redrafts, and stops waiting after 180 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] });
    const { writes } = renderRequests(THREAD.henderson, { redraftNeverLands: true });
    const decision = await card();
    const actions = await composer();
    const approve = () => within(actions).getByRole<HTMLButtonElement>("button", { name: "Approve and send" });

    type(actions, "Shorter, please.");
    fireEvent.keyDown(field(actions), { key: "Enter" });
    expect(await within(decision).findByRole("heading", { name: "Claude is redrafting…" })).toBeTruthy();
    expect(within(decision).getByText(/^Yes, we can fit them in\./)).toBeTruthy();
    expect(approve().disabled).toBe(true);
    expect(REDRAFT_TIMEOUT_MS).toBe(180_000);

    act(() => {
      vi.advanceTimersByTime(REDRAFT_TIMEOUT_MS - 1_000);
    });
    expect(within(actions).queryByRole("alert")).toBeNull();
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect((await within(actions).findByRole("alert")).textContent).toContain("Claude didn't come back with a new draft.");
    expect(approve().disabled).toBe(false);

    // Try again asks for the same redraft (the note is already stored); the first is still
    // running, so the server's 409 has no resolver, and the instruction is kept for later.
    fireEvent.click(within(actions).getByRole("button", { name: "Try again" }));
    const status = await within(actions).findByRole("status");
    expect(within(status).getByText("Claude is still working on an earlier redraft. Try again once it lands.")).toBeTruthy();
    expect(within(status).getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(writes.map((write) => write.route)).toEqual([
      notesRoute(THREAD.henderson),
      redraftRoute(ESCALATION.henderson),
      redraftRoute(ESCALATION.henderson),
    ]);
  });

  it("stores nothing on Enter while a redraft is on its way, keeps the text and says why", async () => {
    const { writes } = renderRequests(THREAD.henderson, { redraftNeverLands: true });
    const decision = await card();
    const actions = await composer();
    type(actions, "Quote the reserve slot only if Raj confirms.");
    fireEvent.keyDown(field(actions), { key: "Enter" });
    expect(await within(decision).findByRole("heading", { name: "Claude is redrafting…" })).toBeTruthy();

    type(actions, "Also suggest Nov 10 instead.");
    expect(within(actions).getByRole<HTMLButtonElement>("button", { name: "Redraft" }).disabled).toBe(true);
    fireEvent.keyDown(field(actions), { key: "Enter" });
    expect((await within(actions).findByRole("status")).textContent).toBe(
      "Claude is still working on an earlier redraft. Try again once it lands.",
    );
    expect(field(actions).value).toBe("Also suggest Nov 10 instead.");
    expect(writes.map((write) => write.route)).toEqual([notesRoute(THREAD.henderson), redraftRoute(ESCALATION.henderson)]);
  });

  it("keeps a redraft the server didn't take for Try again, storing the note once", async () => {
    const { writes } = renderRequests(THREAD.henderson, { failFirst: [redraftRoute(ESCALATION.henderson)] });
    const decision = await card();
    const actions = await composer();
    type(actions, "Shorter, please.");
    fireEvent.keyDown(field(actions), { key: "Enter" });

    const alert = await within(actions).findByRole("alert");
    expect(within(alert).getByText("Claude couldn't redraft this. Edit the draft yourself, or try again.")).toBeTruthy();
    expect(alert.textContent).not.toContain("503");
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await within(decision).findByText("2 of 2")).toBeTruthy();
    expect(within(actions).queryByRole("alert")).toBeNull();
    const asked = { route: redraftRoute(ESCALATION.henderson), body: { instruction: "Shorter, please." } };
    expect(writes).toEqual([{ route: notesRoute(THREAD.henderson), body: { body: "Shorter, please." } }, asked, asked]);
  });

  it("keeps a redraft refused while a teammate's runs for Try again", async () => {
    const { writes } = renderRequests(THREAD.henderson, {
      overrides: {
        "POST /api/escalations/{escalation_id}/redraft": () =>
          problem(409, "escalation_not_open", "escalation can't be redrafted now", { resolved_by: null }),
      },
    });
    const actions = await composer();
    type(actions, "Shorter, please.");
    fireEvent.keyDown(field(actions), { key: "Enter" });

    const status = await within(actions).findByRole("status");
    expect(within(status).getByText("Claude is still working on an earlier redraft. Try again once it lands.")).toBeTruthy();
    fireEvent.click(within(status).getByRole("button", { name: "Try again" }));
    await waitFor(() => {
      expect(writes.map((write) => write.route)).toEqual([
        notesRoute(THREAD.henderson),
        redraftRoute(ESCALATION.henderson),
        redraftRoute(ESCALATION.henderson),
      ]);
    });
    expect(writes.at(-1)?.body).toEqual({ instruction: "Shorter, please." });
  });

  it("keeps a send-back the server didn't take for Try again, storing the note once", async () => {
    const instruction = "Quote the public rate card instead.";
    const { writes } = renderRequests(THREAD.vantage, { failFirst: [resolveRoute(ESCALATION.vantage)] });
    await card();
    const actions = await composer();
    type(actions, instruction);
    fireEvent.click(within(actions).getByRole("button", { name: "Send back to Claude" }));

    const alert = await within(actions).findByRole("alert");
    expect(within(alert).getByText("Couldn't send this back to Claude. Your note is saved; nothing went out.")).toBeTruthy();
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
    const sentBack = { route: resolveRoute(ESCALATION.vantage), body: { action: "revise", text: instruction, draft_rev: 1 } };
    expect(writes).toEqual([{ route: notesRoute(THREAD.vantage), body: { body: instruction } }, sentBack, sentBack]);
  });

  /** A redraft asked for while the budget allowed it, which fails once it reads `budget`. */
  const failRedraft = async (budget: BudgetState) => {
    let state: BudgetState = "ok";
    const { sources, queryClient } = renderRequests(THREAD.henderson, {
      redraftNeverLands: true,
      live: true,
      overrides: { "GET /api/budget": (): BudgetView => ({ live: true, state }) },
    });
    const decision = await card();
    const actions = await composer();
    type(actions, "Shorter, please.");
    fireEvent.keyDown(field(actions), { key: "Enter" });
    expect(await within(decision).findByRole("heading", { name: "Claude is redrafting…" })).toBeTruthy();
    state = budget;
    await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.budget }));
    await waitFor(() => {
      expect(sources.opened).toHaveLength(1);
    });
    const failed = (seq: number, draftRev: number) =>
      durable(
        seq,
        { type: "escalation.redraft_failed", escalation_id: ESCALATION.henderson, draft_rev: draftRev },
        { ...worldScope, kind: "thread", org_id: orgs.northlake.id, thread_id: THREAD.henderson },
      );
    act(() => {
      sources.latest.accept();
      // An earlier rev's failure isn't this redraft's.
      sources.latest.send(failed(10_001, 1));
    });
    expect(within(decision).getByRole("heading", { name: "Claude is redrafting…" })).toBeTruthy();
    act(() => {
      sources.latest.send(failed(10_002, 2));
    });
    return { decision, actions };
  };

  it("stops waiting the moment the worker reports the redraft wrote nothing", async () => {
    const { decision, actions } = await failRedraft("ok");
    expect((await within(actions).findByRole("alert")).textContent).toContain(
      "Claude couldn't redraft this. Edit the draft yourself, or try again.",
    );
    expect(within(decision).queryByRole("heading", { name: "Claude is redrafting…" })).toBeNull();
    expect(within(actions).getByRole<HTMLButtonElement>("button", { name: "Approve and send" }).disabled).toBe(false);
    expect(within(actions).getByRole("button", { name: "Try again" })).toBeTruthy();
  });

  it("says which budget is spent instead of showing a retry", async () => {
    const { actions } = await failRedraft("demo_exhausted");
    await waitFor(() => {
      expect(within(actions).getByRole("alert").textContent).toBe(
        `Claude couldn't redraft this. ${failureLine("demo_budget", "demo_exhausted")}`,
      );
    });
    expect(within(actions).queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("says who got there first when the item was already resolved", async () => {
    renderRequests(THREAD.henderson, { resolvedFirstBy: members.raj });
    const actions = await composer();
    fireEvent.click(within(actions).getByRole("button", { name: "Approve and send" }));
    expect((await within(actions).findByRole("status")).textContent).toBe("Already resolved by Raj Patel.");
  });

  it("doesn't name a teammate when the item changed without anyone resolving it", async () => {
    renderRequests(THREAD.henderson, {
      overrides: {
        "POST /api/escalations/{escalation_id}/resolve": () =>
          problem(409, "escalation_not_open", "escalation is cancelled", { resolved_by: null }),
      },
    });
    const actions = await composer();
    fireEvent.click(within(actions).getByRole("button", { name: "Approve and send" }));
    expect((await within(actions).findByRole("status")).textContent).toMatch(/^This item changed while you were deciding/);
  });
});
