// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import type { MemberView } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import { fetchStreamSnapshot } from "../app/snapshot";
import { sharingPath } from "../customize/sections";
import { fixtureFetch, problem, type FixtureRoutes } from "../fixtures";
import {
  ESCALATION,
  HENDERSON_QUESTION,
  THREAD,
  hendersonDraft,
  requestsRoutes,
  requestsWorld,
  type RequestsRouteOptions,
} from "../fixtures/requests";
import { inbox, members, orgs } from "../fixtures/shell";
import { FixtureProviders } from "../gallery/FixtureProviders";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { durable, EventSources, worldScope } from "../realtime/testing";
import { TooltipProvider } from "../shell/ui";
import { REDRAFT_TIMEOUT_MS } from "./EscalationPanel";
import { InboxRow } from "./InboxList";
import { RoutingLine } from "./RoutingLine";
import { RequestsPage } from "./RequestsPage";

interface Call {
  route: string;
  body: unknown;
}

interface RenderOptions extends RequestsRouteOptions {
  as?: MemberView;
  /** Routes that replace the fixtures' own. */
  overrides?: FixtureRoutes;
}

/** `/requests/:threadId` over the inbox fixtures, recording every write. */
function renderRequests(threadId: string, { overrides, ...options }: RenderOptions = {}) {
  const serve = fixtureFetch({ ...requestsRoutes({ world: requestsWorld(), ...options }), ...overrides });
  const writes: Call[] = [];
  const api = createApiClient({
    baseUrl: "http://harmony.test",
    fetch: async (request) => {
      if (request.method !== "GET") {
        const text = await request.clone().text();
        writes.push({
          route: `${request.method} ${new URL(request.url).pathname}`,
          body: text === "" ? undefined : (JSON.parse(text) as unknown),
        });
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
  const router = createMemoryRouter([{ path: "/requests/:threadId?", element: <RequestsPage /> }], {
    initialEntries: [`/requests/${threadId}`],
  });
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </AppProviders>,
  );
  return { writes, router, sources, dispatcher };
}

const panel = () => screen.findByRole("region", { name: "Needs your decision" });
/** The decision's actions: a composer at the pane's bottom edge, outside the card. */
const actions = () => screen.findByRole("region", { name: "Your decision" });
const inboxSection = (name: string) => screen.findByRole("region", { name });
const resolveRoute = (escalationId: string) => `POST /api/escalations/${escalationId}/resolve`;
const HENDERSON_ANSWER = hendersonDraft("").answer;

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
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it("shows the question, why it needs you, the draft and what Claude looked at, then approves it as shown", async () => {
  const { writes } = renderRequests(THREAD.henderson);

  const decision = await panel();
  const controls = await actions();
  expect(within(decision).getByText(/^Can we run XRD on 12 air-sensitive powders before Nov 6\?/)).toBeTruthy();
  const why = within(decision).getByText("Why this needs you:").parentElement;
  expect(why?.textContent).toContain("Northlake shares what's open on the public calendar, not internal reserve slots.");
  expect(why?.textContent).toContain("(the sharing check)");
  expect(within(decision).getByText(/^Yes, we can fit them in\./)).toBeTruthy();
  const lookedAt = within(decision).getByRole("region", { name: "What Claude looked at" });
  expect(within(lookedAt).getAllByRole("listitem")).toHaveLength(4);
  expect(lookedAt.textContent).toContain("Searched your organization's knowledge · reserve XRD block");
  // Why it's with Helen, and a way to move it, sit beside the decision.
  expect(within(decision).getByText(/^Routed to you by Claude: Instrument priority and reserve-slot requests/)).toBeTruthy();
  expect(within(decision).getByRole("button", { name: "Reassign" })).toBeTruthy();
  // Sharing more from now on is a settings change.
  const link = within(decision).getByRole("link", { name: "Edit what you share with Henderson Lab" });
  expect(link.getAttribute("href")).toBe(sharingPath(orgs.henderson.id));
  expect(within(decision).queryByRole("radiogroup")).toBeNull();
  expect(within(await inboxSection("Needs you")).getByText(/Henderson Lab/)).toBeTruthy();
  // The card reads in full with the transcript: the whole question, the whole draft, no
  // box of its own to scroll. Its actions stay at the pane's bottom edge.
  const question = within(decision).getByRole("heading", { level: 2 });
  expect(question.textContent).toBe(HENDERSON_QUESTION);
  expect(question.className).not.toMatch(/line-clamp|truncate/);
  expect(decision.querySelector(".overflow-y-auto, [class*='max-h-']")).toBeNull();
  expect(decision.contains(controls)).toBe(false);
  expect(screen.getByRole("list", { name: "Thread" }).compareDocumentPosition(decision)).toBe(
    Node.DOCUMENT_POSITION_FOLLOWING,
  );

  fireEvent.click(within(controls).getByRole("button", { name: "Approve & send" }));

  await waitFor(() => {
    expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
  });
  expect(writes).toEqual([
    { route: resolveRoute(ESCALATION.henderson), body: { action: "approve", text: HENDERSON_ANSWER, draft_rev: 1 } },
  ]);
  // As api/routes/inbox.py sorts it: the conversation goes on, so it's Claude's again.
  expect(within(await inboxSection("Claude is handling")).getByText(/Henderson Lab/)).toBeTruthy();
});

it("approves the member's edit", async () => {
  const { writes } = renderRequests(THREAD.henderson);
  const decision = await panel();
  const controls = await actions();
  fireEvent.click(within(controls).getByRole("button", { name: "Edit" }));
  fireEvent.change(within(decision).getByRole("textbox", { name: "Answer" }), { target: { value: "Yes, Wednesday." } });
  fireEvent.click(within(controls).getByRole("button", { name: "Approve & send" }));
  await waitFor(() => {
    expect(writes).toEqual([
      { route: resolveRoute(ESCALATION.henderson), body: { action: "approve", text: "Yes, Wednesday.", draft_rev: 1 } },
    ]);
  });
});

it("states routing under the title, never the thread's status again", () => {
  const { container } = render(<RoutingLine escalation={null} memberId={members.helen.id} sharedBy={null} onReassign={vi.fn()} />);
  expect(container.textContent).toBe("");

  render(
    <RoutingLine
      escalation={null}
      memberId={members.helen.id}
     
      sharedBy={{ name: members.raj.name, question: "Can you check the price?" }}
      onReassign={vi.fn()}
    />,
  );
  expect(screen.getByText(/Raj shared this with you/)).toBeTruthy();
});

it("marks an unread Needs-you row once, with the unread dot", () => {
  const [item] = inbox.needs_you;
  if (item === undefined) throw new Error("no needs-you fixture");
  const { container } = render(
    <FixtureProviders routes={requestsRoutes({ world: requestsWorld() })}>
      <TooltipProvider>
        <InboxRow item={item} section="needs_you" selected={false} unread orgSlug="northlake" onOpen={vi.fn()} />
      </TooltipProvider>
    </FixtureProviders>,
  );
  expect(screen.getAllByLabelText("Unread")).toHaveLength(1);
  expect(container.querySelectorAll(".rounded-full")).toHaveLength(1);
});

it("declines with the reason Claude can pass on", async () => {
  const { writes } = renderRequests(THREAD.henderson);
  const controls = await actions();
  fireEvent.click(within(controls).getByRole("button", { name: "Decline" }));
  fireEvent.change(within(controls).getByRole("textbox", { name: "Reason for declining" }), {
    target: { value: "The reserve block is for internal work." },
  });
  fireEvent.click(within(controls).getByRole("button", { name: "Decline" }));
  await waitFor(() => {
    expect(writes).toEqual([
      {
        route: resolveRoute(ESCALATION.henderson),
        body: { action: "decline", text: "The reserve block is for internal work.", draft_rev: 1 },
      },
    ]);
  });
});

it("shows what a declined question and a declined reply told the representative", async () => {
  const declineWith = async (reason: string) => {
    await panel();
    const controls = await actions();
    fireEvent.click(within(controls).getByRole("button", { name: "Decline" }));
    fireEvent.change(within(controls).getByRole("textbox", { name: "Reason for declining" }), {
      target: { value: reason },
    });
    fireEvent.click(within(controls).getByRole("button", { name: "Decline" }));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
    });
  };
  const released = async () => {
    const [trigger] = await screen.findAllByRole("button", { name: "What our representative saw" });
    if (trigger === undefined) throw new Error("no drawer trigger");
    fireEvent.click(trigger);
    const drawer = await screen.findByRole("dialog", { name: "What our representative saw" });
    return within(drawer).findByRole("region", { name: "Released answers" });
  };

  renderRequests(THREAD.henderson);
  await declineWith("The reserve block is for internal work.");
  const answers = await released();
  expect(within(answers).getByRole("heading", { name: /^Can we run XRD on 12 air-sensitive powders before Nov 6\?/ })).toBeTruthy();
  expect(answers.textContent).toContain("Your side declined to answer. What you may say:");
  expect(answers.textContent).toContain("The reserve block is for internal work.");
  cleanup();

  renderRequests(THREAD.vantage);
  await declineWith("Point them to the public price list.");
  const told = await released();
  expect(within(told).getByRole("heading", { name: "About a message your side held" })).toBeTruthy();
  expect(told.textContent).toContain("Not sent: your side didn't approve it.");
  expect(told.textContent).toContain("Point them to the public price list.");
});

it("asks Claude to redraft and shows the new draft when it lands", async () => {
  const { writes } = renderRequests(THREAD.henderson);
  const decision = await panel();
  const controls = await actions();
  // Beside the decision: the recorded story goes on whatever is decided here.
  expect(screen.getByText(/^Scripted demo: Claude replays the recorded story/)).toBeTruthy();
  const instruction = within(controls).getByRole("textbox", { name: "Redraft instruction" });
  fireEvent.change(instruction, { target: { value: "Shorter, please." } });
  // Typing turns the footer into Redraft.
  expect(within(controls).queryByRole("button", { name: "Approve & send" })).toBeNull();
  fireEvent.click(within(controls).getByRole("button", { name: "Redraft" }));

  expect(await within(decision).findByText(/^Yes\. We can run XRD on Wednesday, Nov 4/)).toBeTruthy();
  expect(within(decision).getByText("2 of 2")).toBeTruthy();
  expect(writes).toEqual([
    { route: `POST /api/escalations/${ESCALATION.henderson}/redraft`, body: { instruction: "Shorter, please." } },
  ]);
});

it("keeps the draft readable but unapprovable while Claude redrafts, and stops waiting if it never lands", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] });
  renderRequests(THREAD.henderson, { redraftNeverLands: true });
  const decision = await panel();
  const controls = await actions();
  const approve = () => within(controls).getByRole<HTMLButtonElement>("button", { name: "Approve & send" });

  const instruction = within(controls).getByRole("textbox", { name: "Redraft instruction" });
  fireEvent.change(instruction, { target: { value: "Shorter, please." } });
  fireEvent.keyDown(instruction, { key: "Enter" });

  expect(await within(decision).findByText("Claude is redrafting…")).toBeTruthy();
  expect(within(decision).getByText(/^Yes, we can fit them in\./)).toBeTruthy();
  expect(approve().disabled).toBe(true);

  act(() => {
    vi.advanceTimersByTime(REDRAFT_TIMEOUT_MS);
  });
  expect((await within(controls).findByRole("alert")).textContent).toContain("Claude didn't come back with a new draft.");
  expect(approve().disabled).toBe(false);

  // Asking again while the first is still running: the server's 409 has no resolver.
  fireEvent.click(within(controls).getByRole("button", { name: "Try again" }));
  const retry = within(controls).getByRole<HTMLInputElement>("textbox", { name: "Redraft instruction" });
  expect(retry.value).toBe("Shorter, please.");
  fireEvent.keyDown(retry, { key: "Enter" });
  expect((await within(controls).findByRole("status")).textContent).toBe(
    "Claude is still working on an earlier redraft. Try again once it lands.",
  );
});

it("stops waiting the moment the worker reports the redraft wrote nothing", async () => {
  const { sources, dispatcher } = renderRequests(THREAD.henderson, { redraftNeverLands: true });
  void dispatcher.start();
  const decision = await panel();
  const controls = await actions();
  const approve = () => within(controls).getByRole<HTMLButtonElement>("button", { name: "Approve & send" });
  const instruction = within(controls).getByRole("textbox", { name: "Redraft instruction" });
  fireEvent.change(instruction, { target: { value: "Shorter, please." } });
  fireEvent.keyDown(instruction, { key: "Enter" });
  expect(await within(decision).findByText("Claude is redrafting…")).toBeTruthy();

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
  expect(within(decision).getByText("Claude is redrafting…")).toBeTruthy();
  expect(approve().disabled).toBe(true);

  act(() => {
    sources.latest.send(failed(10_002, 2));
  });
  expect((await within(controls).findByRole("alert")).textContent).toContain(
    "Claude couldn't redraft this. Edit the draft yourself, or try again.",
  );
  expect(within(decision).queryByText("Claude is redrafting…")).toBeNull();
  expect(approve().disabled).toBe(false);
  fireEvent.click(within(controls).getByRole("button", { name: "Try again" }));
  expect(within(controls).getByRole<HTMLInputElement>("textbox", { name: "Redraft instruction" }).value).toBe(
    "Shorter, please.",
  );
  expect(within(controls).queryByRole("alert")).toBeNull();
});

it("says who got there first when the item was already resolved", async () => {
  renderRequests(THREAD.henderson, { resolvedFirstBy: members.raj });
  const controls = await actions();
  fireEvent.click(within(controls).getByRole("button", { name: "Approve & send" }));
  expect((await within(controls).findByRole("status")).textContent).toBe("Already resolved by Raj Patel.");
});

it("doesn't name a teammate when the item changed without anyone resolving it", async () => {
  renderRequests(THREAD.henderson, {
    overrides: {
      "POST /api/escalations/{escalation_id}/resolve": () =>
        problem(409, "escalation_not_open", "escalation is cancelled", { resolved_by: null }),
    },
  });
  const controls = await actions();
  fireEvent.click(within(controls).getByRole("button", { name: "Approve & send" }));
  expect((await within(controls).findByRole("status")).textContent).toMatch(/^This item changed while you were deciding/);
});

it("reviews a held reply: no redraft, and approving sends it", async () => {
  const { writes } = renderRequests(THREAD.vantage);
  const decision = await panel();
  const controls = await actions();
  expect(within(decision).getByText("Review Claude's reply to Vantage Market Intelligence")).toBeTruthy();
  expect(within(decision).getByText("Why this needs you:").parentElement?.textContent).toContain(
    "A teammate approves every message in Ask mode.",
  );
  expect(within(controls).queryByRole("textbox", { name: "Redraft instruction" })).toBeNull();
  // A held message's rationale is its reason; it isn't repeated.
  expect(within(decision).queryByRole("button", { name: "Claude's reasoning" })).toBeNull();
  expect(within(decision).getByText("Approving sends this reply to Vantage Market Intelligence.")).toBeTruthy();

  fireEvent.click(within(controls).getByRole("button", { name: "Approve & send" }));

  await waitFor(() => {
    expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
  });
  expect(writes.map((write) => write.route)).toEqual([resolveRoute(ESCALATION.vantage)]);
  const thread = await screen.findByRole("list", { name: "Thread" });
  expect(await within(thread).findByText(/^We don't share who books our instruments/)).toBeTruthy();
});

it("reassigns in one click", async () => {
  const { writes } = renderRequests(THREAD.henderson);
  const decision = await panel();
  fireEvent.click(within(decision).getByRole("button", { name: "Reassign" }));
  const picker = await screen.findByRole("listbox", { name: "Reassign to" });
  fireEvent.click(await within(picker).findByRole("option", { name: /Raj Patel/ }));
  await waitFor(() => {
    expect(writes).toEqual([
      { route: `POST /api/escalations/${ESCALATION.henderson}/reassign`, body: { member_id: members.raj.id } },
    ]);
  });
  // Raj has it now; the line says so instead of Claude's routing reason.
  expect((await within(decision).findAllByText("Assigned to Raj")).length).toBeGreaterThan(0);
  // In Helen's list it waits on her teammate, not on Claude.
  const team = await inboxSection("With your team");
  expect(within(team).getByText("Henderson Lab · Waiting on Raj")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Claude is handling" })?.textContent ?? "").not.toMatch(/Henderson Lab/);
});

it("posts an internal note that never reaches the counterparty", async () => {
  const { writes } = renderRequests(THREAD.vantage);
  const box = await screen.findByRole("textbox", { name: "Internal note" });
  fireEvent.focus(box);
  fireEvent.change(box, { target: { value: "Their budget is fixed this quarter." } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
    await Promise.resolve();
  });
  await waitFor(() => {
    expect(writes.map((write) => write.route)).toEqual([`POST /api/threads/${THREAD.vantage}/notes`]);
  });
  expect(await screen.findByText("Their budget is fixed this quarter.")).toBeTruthy();
});

it("shows a closed thread with our own summary", async () => {
  renderRequests(THREAD.ridgeline);
  const ended = await screen.findByRole("region", { name: "Closed" });
  expect(ended.textContent).toContain("Ridgeline Fabrication booked SEM on four machined PEEK holders");
  expect(ended.textContent).toContain("Only Northlake Characterization Core sees it.");
  expect(screen.getByRole("separator", { name: "Claude for Ridgeline Fabrication closed the thread" })).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Needs your decision" })).toBeNull();
});

it("opens what our representative saw", async () => {
  renderRequests(THREAD.henderson);
  // The header's eye button; the properties rail has the same link on wide screens.
  const [trigger] = await screen.findAllByRole("button", { name: "What our representative saw" });
  if (trigger === undefined) throw new Error("no drawer trigger");
  fireEvent.click(trigger);
  const drawer = await screen.findByRole("dialog", { name: "What our representative saw" });
  const prompt = await within(drawer).findByRole("region", { name: "Its prompt" });
  expect(prompt.textContent).toContain("What it may pass on");
  // Raj's note never reached it: nothing has been released on this thread yet.
  expect(drawer.textContent).not.toContain("reserve XRD block Wed");
});
