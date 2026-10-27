// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import { queryKeys } from "../api/queryKeys";
import type { EntitySnapshot, EventEnvelope, Inbox } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import { fetchStreamSnapshot } from "../app/snapshot";
import { fixtureFetch, problem, type FixtureRoutes } from "../fixtures";
import {
  cardRecipients,
  chatDeltaEnvelope,
  chatRoutes,
  chatStory,
  conversationAt,
  CONVERSATION_ID,
  initialState,
  NORTHLAKE_GO_AHEAD_ID,
  NORTHLAKE_THREAD_ID,
  OUTREACH_CARD_ID,
  REQUEST_TITLE,
  requestAt,
  RIDGELINE_GO_AHEAD_ID,
  SEAL_QUESTION_ID,
  SUMMARIES,
  TURN,
  type ChatOptions,
  type ChatStage,
} from "../fixtures/chat";
import { hendersonSettings } from "../fixtures/customize";
import { alexInbox, budgets, FIXTURE_NOW, members, orgs, sessionAs, shellRoutes, WORLD_ID } from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { SidebarRequests } from "../requests/SidebarRequests";
import { EventSources } from "../realtime/testing";
import { ARRIVAL_PAUSE_MS } from "./cards/AskUserCard";
import { OutreachCard } from "./cards/OutreachCard";
import { RequestStatusPanel } from "./cards/RequestStatusCard";
import { Markdown } from "./Markdown";
import { failureLine, stoppedLine, type FailureReason } from "./failureCopy";
import { optimisticStore } from "./optimistic";
import { ChatPage, NewChatPage } from "./pages";
import type { CardRecipient, ConversationDetail, OutreachCardItem } from "./types";

interface Call {
  route: string;
  body: unknown;
}

async function bodyOf(request: Request): Promise<unknown> {
  const text = await request.clone().text();
  return text === "" ? undefined : (JSON.parse(text) as unknown);
}

/** `/chat/:id` over the story's fixtures at `stage`, recording every write. */
function renderChat(stage: ChatStage, options: ChatOptions = {}) {
  return renderRoutes(chatRoutes(stage, options));
}

/** `element` at `url` (Alex's chat by default) in the app's providers, recording every write. */
function renderRoutes(
  routes: FixtureRoutes,
  {
    element = <ChatPage />,
    path = "/chat/:id",
    url = `/chat/${CONVERSATION_ID}`,
  }: { element?: ReactNode; path?: string; url?: string } = {},
) {
  const serve = fixtureFetch({ ...shellRoutes(), ...routes });
  const writes: Call[] = [];
  const api = createApiClient({
    baseUrl: "http://harmony.test",
    fetch: async (request) => {
      if (request.method !== "GET") {
        writes.push({ route: `${request.method} ${new URL(request.url).pathname}`, body: await bodyOf(request) });
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
  void dispatcher.start();
  const router = createMemoryRouter(
    [
      { path, element },
      { path: "*", element: <p>Elsewhere</p> },
    ],
    { initialEntries: [url] },
  );
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { writes, sources, dispatcher, queryClient };
}

const askCard = () => screen.findByRole("listbox", { name: "Send this request to 2 organizations?" });
const composer = () => screen.findByRole("textbox", { name: "Write your message to Claude" });
const APPROVE = `POST /api/cards/${OUTREACH_CARD_ID}/approve`;

let seq = 100;
function snapshotEnvelope(entityType: string, entityId: string, data: EntitySnapshot["data"] = {}): EventEnvelope {
  seq += 1;
  return {
    world_id: WORLD_ID,
    seq,
    created_at: FIXTURE_NOW,
    scope: { kind: "world", org_id: null, member_id: null, thread_id: null, session_id: null },
    event: { type: "entity.snapshot", entity_type: entityType, entity_id: entityId, version: seq, data },
  };
}

/** The server says `entity` changed, over the tab's event stream. */
async function announce(sources: EventSources, entityType: string, entityId: string): Promise<void> {
  await waitFor(() => {
    expect(sources.opened).toHaveLength(1);
  });
  act(() => {
    sources.latest.accept();
    sources.latest.send(snapshotEnvelope(entityType, entityId));
  });
}

beforeEach(() => {
  // Measures once, on observe: enough for the clamps under `simulateLayout`.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      readonly #callback: (entries: readonly unknown[]) => void;
      constructor(callback: (entries: readonly unknown[]) => void) {
        this.#callback = callback;
      }
      observe(): void {
        this.#callback([]);
      }
      unobserve(): void {}
      disconnect(): void {}
    },
  );
  // jsdom doesn't scroll.
  Element.prototype.scrollTo = () => undefined;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  optimisticStore.setState({ byConversation: {} });
});

/**
 * jsdom lays nothing out, so a rough layout stands in: 80 characters to a line. A
 * `-webkit-line-clamp` box shows at most its lines, and a textarea is as tall as its
 * style says, so long text is cut off and short text fits.
 */
function simulateLayout(): void {
  const textHeight = (element: HTMLElement) => {
    const textarea = element instanceof HTMLTextAreaElement;
    const text = textarea ? element.value : element.textContent;
    const lines = text.split("\n").reduce((sum, line) => sum + Math.max(1, Math.ceil(line.length / 80)), 0);
    return lines * (textarea ? 22 : 20);
  };
  const boxHeight = (element: HTMLElement) => {
    if (element instanceof HTMLTextAreaElement) return parseFloat(element.style.height) || 0;
    const clamp = Number(element.style.webkitLineClamp);
    return clamp > 0 ? Math.min(textHeight(element), clamp * 20) : textHeight(element);
  };
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return boxHeight(this);
  });
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
    return boxHeight(this);
  });
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return Math.max(textHeight(this), boxHeight(this));
  });
}

it("asks before sending the request, and on Enter sends it to the organizations that fit", async () => {
  const { writes } = renderChat("drafted");
  const list = await askCard();
  // Calder was screened as not a fit: listed for Alex to choose, but not sent by default.
  const first = within(list).getByRole("option", { name: /Send to 2 organizations/ });
  expect(first.getAttribute("aria-selected")).toBe("true");
  expect(first.textContent).not.toContain("Calder");

  fireEvent.keyDown(list, { key: "Enter" });

  await screen.findByText("Sent to Northlake and Ridgeline.", { exact: false });
  expect(writes).toEqual([
    { route: APPROVE, body: { recipients: [orgs.northlake.id, orgs.ridgeline.id] } },
  ]);
  expect(screen.queryByRole("listbox")).toBeNull();
  // The request's live status loads into the transcript.
  expect(await screen.findByText("Open in Requests")).toBeTruthy();
});

it("sends to a subset, with a not-a-fit organization unticked until chosen", async () => {
  const { writes } = renderChat("drafted");
  fireEvent.click(within(await askCard()).getByRole("option", { name: /Send to a subset/ }));
  expect(screen.getByRole<HTMLInputElement>("checkbox", { name: /Calder Computational/ }).checked).toBe(false);
  fireEvent.click(screen.getByRole("checkbox", { name: /Ridgeline Fabrication/ }));
  fireEvent.click(screen.getByRole("button", { name: "Send to 1 organization" }));

  await waitFor(() => {
    expect(writes).toEqual([
      { route: `POST /api/cards/${OUTREACH_CARD_ID}/approve`, body: { recipients: [orgs.northlake.id] } },
    ]);
  });
});

it("takes \"Something else\" as the card's rejection feedback", async () => {
  const { writes } = renderChat("drafted");
  await askCard();
  fireEvent.change(screen.getByRole("textbox", { name: "Something else" }), {
    target: { value: "Leave Ridgeline out" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  await screen.findAllByText("Not sent");
  expect(writes).toEqual([
    { route: `POST /api/cards/${OUTREACH_CARD_ID}/reject`, body: { feedback: "Leave Ridgeline out" } },
  ]);
  expect(screen.getByText("Leave Ridgeline out", { exact: false })).toBeTruthy();
});

it("puts the card away on Esc, and brings it back from the composer's strip", async () => {
  renderChat("drafted");
  fireEvent.keyDown(await askCard(), { key: "Escape" });

  const review = await screen.findByRole("button", { name: /Send this request to 2 organizations\?\s*Review/ });
  expect(screen.getByRole("textbox", { name: "Write your message to Claude" })).toHaveProperty(
    "placeholder",
    "Tell Claude what to change instead…",
  );
  fireEvent.click(review);
  await askCard();
});

it("answers a counterparty's question inline", async () => {
  const { writes } = renderChat("question");
  const answer = await screen.findByRole("textbox", { name: "Your answer about Ridgeline Fabrication" });
  fireEvent.change(answer, { target: { value: "Kalrez, please." } });
  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));

  await screen.findByText("Answered");
  expect(writes).toEqual([
    { route: `POST /api/escalations/${SEAL_QUESTION_ID}/resolve`, body: { action: "approve", text: "Kalrez, please." } },
  ]);
});

describe("a question answered in this chat", () => {
  const [asked] = alexInbox.needs_you;
  if (asked === undefined) throw new Error("no question of Alex's in the fixtures");
  const row = `${asked.title}, with ${asked.counterparty.name}`;
  const renderWithSidebar = (inbox: Inbox) => {
    window.localStorage.clear();
    renderRoutes(
      { ...chatRoutes("question"), "GET /api/inbox": inbox },
      {
        element: (
          <>
            <SidebarRequests />
            <ChatPage />
          </>
        ),
      },
    );
  };

  it("is seen once the chat it is answered in is open, so its Needs-you row stops reading unread", async () => {
    renderWithSidebar(alexInbox);
    expect(await screen.findByRole("link", { name: row })).toBeTruthy();
    expect(screen.queryByRole("link", { name: `${row}, unread` })).toBeNull();
  });

  it("stays unread while another chat is open", async () => {
    renderWithSidebar({ ...alexInbox, needs_you: [{ ...asked, conversation_id: "0b1d7a3e-0000-4000-8000-0000000002ff" }] });
    await screen.findByRole("textbox", { name: "Your answer about Ridgeline Fabrication" });
    expect(await screen.findByRole("link", { name: `${row}, unread` })).toBeTruthy();
  });
});

/** Answers the open question about `org` in its own card (an answered one has no box). */
async function answerAbout(org: string, text: string): Promise<void> {
  const cards = await screen.findAllByRole("region", { name: `Your representative asks about ${org}` });
  const card = cards.find((candidate) => within(candidate).queryByRole("textbox") !== null);
  if (card === undefined) throw new Error(`no open question about ${org}`);
  fireEvent.change(within(card).getByRole("textbox"), { target: { value: text } });
  fireEvent.click(within(card).getByRole("button", { name: "Send answer" }));
  await within(card).findByText("Answered");
}

it("answers go-aheads on proposed terms, and sums up once both threads close", async () => {
  const { writes } = renderChat("go_ahead");
  await answerAbout("Northlake Characterization Core", "Yes, go ahead.");
  expect(screen.queryByText(/inside your \$2,000 budget/)).toBeNull();
  await answerAbout("Ridgeline Fabrication", "Yes, with the Kalrez seal.");

  expect(await screen.findByText(/inside your \$2,000 budget/)).toBeTruthy();
  expect(writes).toEqual([
    { route: `POST /api/escalations/${NORTHLAKE_GO_AHEAD_ID}/resolve`, body: { action: "approve", text: "Yes, go ahead." } },
    {
      route: `POST /api/escalations/${RIDGELINE_GO_AHEAD_ID}/resolve`,
      body: { action: "approve", text: "Yes, with the Kalrez seal." },
    },
  ]);
});

it("says responses will arrive while the request is active, and stops once it closes", async () => {
  renderChat("sent");
  expect(await screen.findByText("Responses from the network will appear here as they arrive.")).toBeTruthy();
  cleanup();

  renderChat("closed");
  await screen.findByText(/inside your \$2,000 budget/);
  await waitFor(() => {
    expect(screen.getAllByText("Closed").length).toBeGreaterThan(0);
  });
  expect(screen.queryByText("Responses from the network will appear here as they arrive.")).toBeNull();
});

it("streams the turn's text from chat deltas while Claude works", async () => {
  const { sources } = renderChat("working");
  await screen.findByText("Searching the Harmony network");
  await waitFor(() => {
    expect(sources.opened).toHaveLength(1);
  });
  act(() => {
    sources.latest.accept();
    sources.latest.send(chatDeltaEnvelope(TURN.search, 1, "I screened the network "));
    sources.latest.send(chatDeltaEnvelope(TURN.search, 1, "and found two organizations"));
  });
  expect(await screen.findByText("I screened the network and found two organizations")).toBeTruthy();

  // A retried attempt replaces the partial text.
  act(() => {
    sources.latest.send(chatDeltaEnvelope(TURN.search, 2, "Retried"));
  });
  expect(await screen.findByText("Retried")).toBeTruthy();
  expect(screen.queryByText(/found two organizations/)).toBeNull();
});

it("leaves the card up on Enter over its dismiss buttons, which only put it away", async () => {
  const { writes } = renderChat("drafted");
  await askCard();
  for (const name of ["Skip for now", "Skip"]) {
    fireEvent.keyDown(screen.getByRole("button", { name }), { key: "Enter" });
    expect(screen.getByRole("listbox")).toBeTruthy();
  }
  // The browser turns Enter on a button into a click.
  fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  await screen.findByRole("button", { name: /Review/ });
  expect(writes).toEqual([]);
});

it("goes back from a panel on Esc, keeping what was chosen", async () => {
  renderChat("drafted");
  fireEvent.click(within(await askCard()).getByRole("option", { name: /Send to a subset/ }));
  expect(document.activeElement).toBe(screen.getByRole("checkbox", { name: /Northlake/ }));
  const ridgeline = screen.getByRole("checkbox", { name: /Ridgeline Fabrication/ });
  fireEvent.click(ridgeline);
  fireEvent.keyDown(ridgeline, { key: "Escape" });

  const list = await screen.findByRole("listbox", { name: "Send this request to 1 organization?" });
  expect(document.activeElement).toBe(list);
  fireEvent.click(within(list).getByRole("option", { name: /Send to a subset/ }));
  expect(screen.getByRole("checkbox", { name: /Ridgeline Fabrication/ })).toHaveProperty("checked", false);
});

it("keeps \"Something else\" while the card is put away", async () => {
  renderChat("drafted");
  await askCard();
  const other = screen.getByRole("textbox", { name: "Something else" });
  fireEvent.change(other, { target: { value: "Leave Ridgeline" } });
  fireEvent.keyDown(other, { key: "Escape" });
  fireEvent.click(await screen.findByRole("button", { name: /Review/ }));
  await askCard();
  expect(screen.getByRole("textbox", { name: "Something else" })).toHaveProperty("value", "Leave Ridgeline");
});

it("sends an edited brief with only what changed", async () => {
  const { writes } = renderChat("drafted");
  fireEvent.click(within(await askCard()).getByRole("option", { name: /Edit brief/ }));
  const brief = screen.getByRole("textbox", { name: "Brief" });
  const send = screen.getByRole("button", { name: "Send with changes" });
  expect(send).toHaveProperty("disabled", true);
  // Limits live in each organization's ask, which only that conversation's representative reads (3.13 E2).
  const hint = brief.getAttribute("aria-describedby");
  expect(hint === null ? "" : document.getElementById(hint)?.textContent).toContain(
    "A budget or other limit goes in each organization's ask, which only your representative in that conversation sees.",
  );
  fireEvent.change(brief, { target: { value: `${(brief as HTMLTextAreaElement).value}\n\nResults by Nov 4 if you can.  ` } });
  fireEvent.click(send);

  await waitFor(() => {
    expect(writes).toHaveLength(1);
  });
  const [write] = writes;
  expect(write?.route).toBe(APPROVE);
  expect(write?.body).toEqual({
    recipients: [orgs.northlake.id, orgs.ridgeline.id],
    brief: expect.stringMatching(/^XRD and SEM\/EDS[\s\S]*Results by Nov 4 if you can\.$/) as unknown,
  });
});

it("takes a message sent while the card is put away as the card's feedback", async () => {
  const { writes } = renderChat("drafted");
  fireEvent.keyDown(await askCard(), { key: "Escape" });
  const box = await composer();
  fireEvent.change(box, { target: { value: "Leave Ridgeline out" } });
  fireEvent.keyDown(box, { key: "Enter" });

  await screen.findAllByText("Not sent");
  expect(writes).toEqual([
    { route: `POST /api/conversations/${CONVERSATION_ID}/messages`, body: { text: "Leave Ridgeline out" } },
  ]);
  // On the card, not as a message.
  expect(screen.getByText("Leave Ridgeline out", { exact: false })).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Review/ })).toBeNull();
});

it("keeps a card that arrives mid-draft behind the composer, with the draft", async () => {
  const story = chatStory("working");
  const { writes, sources } = renderRoutes(story.routes);
  fireEvent.change(await composer(), { target: { value: "Also ask Calder" } });
  story.advance("drafted");
  await announce(sources, "conversation", CONVERSATION_ID);

  await screen.findByRole("button", { name: /Send this request to 2 organizations\?\s*Review/ });
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(await composer()).toHaveProperty("value", "Also ask Calder");
  expect(writes).toEqual([]);
});

it("opens a card that arrives over an empty composer, ignoring keys for a moment", async () => {
  const story = chatStory("working");
  const { writes, sources } = renderRoutes(story.routes);
  await composer();
  story.advance("drafted");
  await announce(sources, "conversation", CONVERSATION_ID);

  const list = await askCard();
  expect(document.activeElement).toBe(list);
  fireEvent.keyDown(list, { key: "1" });
  fireEvent.keyDown(list, { key: "Enter" });
  await act(() => new Promise((resolve) => setTimeout(resolve, ARRIVAL_PAUSE_MS + 50)));
  expect(writes).toEqual([]);

  fireEvent.keyDown(list, { key: "Enter" });
  await waitFor(() => {
    expect(writes).toEqual([{ route: APPROVE, body: { recipients: [orgs.northlake.id, orgs.ridgeline.id] } }]);
  });
});

it("refreshes the request's status when one of its threads changes", async () => {
  const story = chatStory("sent");
  const { sources } = renderRoutes(story.routes);
  await screen.findByText("Open in Requests");
  expect(screen.queryByText("Needs your answer")).toBeNull();

  story.advance("question");
  await announce(sources, "thread", NORTHLAKE_THREAD_ID);
  expect(await screen.findByText("Needs your answer")).toBeTruthy();
});

it("clears a failed answer's error when the member tries again", async () => {
  const story = chatStory("question");
  const answer = story.routes["POST /api/escalations/{escalation_id}/resolve"];
  if (typeof answer !== "function") throw new Error("the fixture answers through a handler");
  let failures = 1;
  renderRoutes({
    ...story.routes,
    "POST /api/escalations/{escalation_id}/resolve": (request) => {
      failures -= 1;
      return failures >= 0 ? problem(503, "unavailable", "Couldn't reach Ridgeline.") : answer(request);
    },
  });
  fireEvent.change(await screen.findByRole("textbox", { name: "Your answer about Ridgeline Fabrication" }), {
    target: { value: "Kalrez, please." },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Couldn't reach Ridgeline.");

  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
  expect(screen.queryByRole("alert")).toBeNull();
  await screen.findByText("Answered");
});

it("says when Claude couldn't reply, and not while it is working again", async () => {
  const failed = { ...conversationAt(initialState("closed")), turn_failure: "model_error" as const };
  const line = failureLine("model_error", "ok");
  renderRoutes({ ...chatRoutes("closed"), "GET /api/conversations/{conversation_id}": () => failed });
  expect((await screen.findByText(line)).getAttribute("role")).toBe("alert");
  cleanup();

  renderRoutes({
    ...chatRoutes("closed"),
    "GET /api/conversations/{conversation_id}": () => ({ ...failed, turn_in_flight: true }),
  });
  await composer();
  expect(screen.queryByText(line)).toBeNull();
});

it("says a budget stop by what the budget says now", async () => {
  renderRoutes({ ...chatRoutes("sent", { turnFailure: "daily_budget" }), "GET /api/budget": budgets.dailyExhausted });
  expect(await screen.findByText("Today's live-model budget is used up. It resets at 00:00 UTC.")).toBeTruthy();
  cleanup();

  // Spent no longer: the next try may well go through.
  renderChat("sent", { turnFailure: "daily_budget" });
  expect(await screen.findByText(failureLine("daily_budget", "ok"))).toBeTruthy();
});

it("never loads images in Claude's text", () => {
  const text = "See ![the chart](https://elsewhere.test/c.png?d=NaMnFeNi) and [the quote](https://elsewhere.test/q).";
  const { container } = render(<Markdown>{text}</Markdown>);
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText("[the chart]")).toBeTruthy();
  expect(screen.getByRole("link", { name: "the quote" })).toBeTruthy();
});

it("says so when the chat isn't there", async () => {
  const serve = fixtureFetch({ ...shellRoutes(), ...chatRoutes("drafted") });
  const api = createApiClient({ baseUrl: "http://harmony.test", fetch: serve });
  const queryClient = createQueryClient();
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
    openEventSource: new EventSources().open,
    openChannel: () => null,
    reload: vi.fn(),
  });
  void dispatcher.start();
  const router = createMemoryRouter([{ path: "/chat/:id", element: <ChatPage /> }], {
    initialEntries: ["/chat/0b1d7a3e-0000-4000-8000-00000000ffff"],
  });
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  expect(await screen.findByText("This chat isn't here")).toBeTruthy();
});

// --- The live demo's failure states (LIVE_DEMO_PLAN 3.5) ---------------------------------

const BLOCKED = [
  ["session_exhausted", budgets.sessionExhausted, "Session budget used up"],
  ["daily_exhausted", budgets.dailyExhausted, "Today's demo budget is used up"],
  ["demo_exhausted", budgets.demoExhausted, "The demo's live-model budget is used up"],
  ["paused", budgets.paused, "Live models are paused"],
] as const;

it.each(BLOCKED)("disables the composer while the budget is %s, and says so", async (_state, budget, line) => {
  const { writes } = renderRoutes({ ...chatRoutes("sent"), "GET /api/budget": budget });
  expect(await screen.findByText(line)).toBeTruthy();
  const box = await composer();
  expect(box).toHaveProperty("disabled", true);
  // The draft stays, and nothing goes out.
  fireEvent.change(box, { target: { value: "Any news?" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(screen.getByRole("button", { name: "Send message" })).toHaveProperty("disabled", true);
  expect(writes).toEqual([]);
});

it("enables the composer while the budget is ok, and again once a cap resets", async () => {
  let budget: (typeof budgets)[keyof typeof budgets] = budgets.dailyExhausted;
  const { writes, queryClient } = renderRoutes({ ...chatRoutes("sent"), "GET /api/budget": () => budget });
  await screen.findByText("Today's demo budget is used up");
  expect(await composer()).toHaveProperty("disabled", true);

  budget = budgets.ok;
  await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.budget }));
  await waitFor(() => {
    expect(screen.queryByText("Today's demo budget is used up")).toBeNull();
  });
  const box = await composer();
  expect(box).toHaveProperty("disabled", false);
  fireEvent.change(box, { target: { value: "Any news?" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => {
    expect(writes).toEqual([{ route: `POST /api/conversations/${CONVERSATION_ID}/messages`, body: { text: "Any news?" } }]);
  });
});

// Sending a request or answering a question hands the move to a representative, whose
// turn would be refused and its thread ended for good: those hold too (3.4).
const HOLDS = [
  ["paused", budgets.paused, "Live models are paused"],
  ["session_exhausted", budgets.sessionExhausted, "Session budget used up"],
] as const;

it.each(HOLDS)("holds every send on the outreach card while the budget is %s, and says so", async (_state, budget, line) => {
  const { writes } = renderRoutes({ ...chatRoutes("drafted"), "GET /api/budget": budget });
  const list = await askCard();
  expect(await screen.findByText(line)).toBeTruthy();
  const send = within(list).getByRole("option", { name: /Send to 2 organizations/ });
  expect(send.getAttribute("aria-disabled")).toBe("true");
  fireEvent.keyDown(list, { key: "Enter" });
  fireEvent.click(send);

  // "Something else" would start Claude's turn as the composer would; it holds the same way.
  fireEvent.change(screen.getByRole("textbox", { name: "Something else" }), { target: { value: "Leave Ridgeline out" } });
  expect(screen.getByRole("button", { name: "Send" })).toHaveProperty("disabled", true);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Something else" }), { key: "Enter" });

  // Choosing and rewording still work; only the sends hold.
  fireEvent.click(within(list).getByRole("option", { name: /Send to a subset/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Ridgeline Fabrication/ }));
  expect(screen.getByRole("button", { name: "Send to 1 organization" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: /Edit brief/ }));
  fireEvent.change(screen.getByRole("textbox", { name: "Brief" }), { target: { value: "A shorter brief." } });
  expect(screen.getByRole("button", { name: "Send with changes" })).toHaveProperty("disabled", true);

  expect(writes).toEqual([]);
});

it.each(HOLDS)("holds an inline answer while the budget is %s, and says so", async (_state, budget, line) => {
  const { writes } = renderRoutes({ ...chatRoutes("question"), "GET /api/budget": budget });
  const answer = await screen.findByRole("textbox", { name: "Your answer about Ridgeline Fabrication" });
  const card = screen.getByRole("region", { name: "Your representative asks about Ridgeline Fabrication" });
  expect(await within(card).findByText(line)).toBeTruthy();
  // The answer stays, and nothing goes out.
  fireEvent.change(answer, { target: { value: "Kalrez, please." } });
  expect(within(card).getByRole("button", { name: "Send answer" })).toHaveProperty("disabled", true);
  fireEvent.keyDown(answer, { key: "Enter", metaKey: true });
  expect(answer).toHaveProperty("value", "Kalrez, please.");
  expect(writes).toEqual([]);
});

it("sends the outreach card once the budget is ok again", async () => {
  let budget: (typeof budgets)[keyof typeof budgets] = budgets.paused;
  const { writes, queryClient } = renderRoutes({ ...chatRoutes("drafted"), "GET /api/budget": () => budget });
  const list = await askCard();
  await screen.findByText("Live models are paused");
  fireEvent.keyDown(list, { key: "Enter" });
  expect(writes).toEqual([]);

  budget = budgets.ok;
  await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.budget }));
  await waitFor(() => {
    expect(screen.queryByText("Live models are paused")).toBeNull();
  });
  fireEvent.keyDown(await askCard(), { key: "Enter" });
  await waitFor(() => {
    expect(writes).toEqual([{ route: APPROVE, body: { recipients: [orgs.northlake.id, orgs.ridgeline.id] } }]);
  });
});

/** The request's status card on its own, as `renderRoutes` serves it. */
async function statusCard(view: ReturnType<typeof requestAt>, routes: FixtureRoutes = {}): Promise<HTMLElement> {
  renderRoutes(routes, { element: <RequestStatusPanel view={view} memberId={members.alex.id} />, path: "/", url: "/" });
  return screen.findByRole("region", { name: "Harmony Network Request" });
}

const REASONS: readonly FailureReason[] = ["session_budget", "daily_budget", "demo_budget", "paused", "model_error", "refused"];

it.each(REASONS)("says why the system stopped a thread (%s) in place of its summary", async (reason) => {
  const card = await statusCard(requestAt(initialState("sent", { stopReason: reason })));
  const ridgeline = within(card).getByRole("link", { name: /Ridgeline Fabrication/ });
  expect(within(ridgeline).getByText(stoppedLine(reason, "ok"))).toBeTruthy();
  expect(within(ridgeline).getByText("Closed")).toBeTruthy();
});

it("shows the summary our side wrote after the other side refused, and why it stopped", async () => {
  const view = requestAt(initialState("sent", { stopReason: "refused" }));
  const summary = "Ridgeline declined to continue; no holder quote.";
  const card = await statusCard({
    ...view,
    recipients: view.recipients.map((row) =>
      row.org.name === "Ridgeline Fabrication" ? { ...row, close_summary: summary } : row,
    ),
  });
  const ridgeline = within(card).getByRole("link", { name: /Ridgeline Fabrication/ });
  expect(within(ridgeline).getByText(summary)).toBeTruthy();
  expect(within(ridgeline).getByText(stoppedLine("refused", "ok"))).toBeTruthy();
});

it("reads a budget stop by the budget's state now", async () => {
  const card = await statusCard(requestAt(initialState("sent", { stopReason: "daily_budget" })), {
    "GET /api/budget": budgets.dailyExhausted,
  });
  expect(await within(card).findByText(stoppedLine("daily_budget", "daily_exhausted"))).toBeTruthy();
});

it("shows a closed thread's summary when nothing stopped it", async () => {
  const card = await statusCard(requestAt(initialState("closed")));
  expect(within(card).getByText(SUMMARIES.henderson.northlake)).toBeTruthy();
  expect(within(card).getByText(SUMMARIES.henderson.ridgeline)).toBeTruthy();
});

it("counts only the organizations the request went to", async () => {
  const card = await statusCard(requestAt(initialState("sent")));
  expect(within(card).getByText("Sent to 2 · 1 not sent")).toBeTruthy();
  const calder = within(card).getByText("Calder Computational").closest("li");
  expect(calder === null ? null : within(calder).getByText("Not sent")).toBeTruthy();
  expect(within(card).queryByText("Screened")).toBeNull();
});

it("names the request's card Harmony Network Request, with the request's title in its body", async () => {
  const card = await statusCard(requestAt(initialState("sent")));
  expect(within(card).getByText(REQUEST_TITLE)).toBeTruthy();
  cleanup();

  render(<OutreachCard item={outreachCardAt(conversationAt(initialState("sent")))} />);
  const outreach = screen.getByRole("region", { name: "Harmony Network Request" });
  expect(within(outreach).getByText(REQUEST_TITLE)).toBeTruthy();
  // Each organization's subject, as it will read it.
  expect(within(outreach).getByText("Airtight holder quote")).toBeTruthy();
});

// --- Sending without review (LIVE_DEMO_PLAN 3.10) --------------------------------------

function outreachCardAt(detail: ConversationDetail): OutreachCardItem {
  const card = detail.items.find((item): item is OutreachCardItem => item.type === "outreach_card");
  if (card === undefined) throw new Error("no outreach card in the fixture");
  return card;
}

it.each([
  ["pending", initialState("drafted", { auto: true }), "Sending…"],
  ["approved", initialState("sent", { auto: true }), "Sent without review"],
  ["rejected", initialState("drafted", { auto: true, autoFailure: "Ridgeline wasn't screened as a fit." }), "Not sent"],
] as const)("shows an unreviewed card %s with its line and nothing to press", (status, state, line) => {
  const item = outreachCardAt(conversationAt(state));
  expect(item.status).toBe(status);
  render(<OutreachCard item={item} />);
  const card = screen.getByRole("region", { name: "Harmony Network Request" });
  expect(within(card).getByText(line)).toBeTruthy();
  expect(within(card).queryAllByRole("button")).toEqual([]);
  if (status === "rejected") expect(within(card).getByText("Not sent: Ridgeline wasn't screened as a fit.")).toBeTruthy();
});

it("asks nothing while Claude sends without review", async () => {
  renderChat("drafted", { auto: true, override: true });
  expect(await screen.findByText("Sending…")).toBeTruthy();
  await composer();
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(screen.queryByRole("button", { name: /Review/ })).toBeNull();
});

const chip = () => screen.queryByRole("button", { name: "Sends without review" });

it.each([
  [{}, false],
  [{ orgDefault: true }, true],
  [{ orgDefault: true, override: false }, false],
  [{ override: true }, true],
] as const)("shows the chip exactly while the chat sends without review (%o)", async (options, shown) => {
  renderChat("sent", options);
  await composer();
  await waitFor(() => {
    expect(chip() !== null).toBe(shown);
  });
});

it("says in words that the chat sends without review, at phone width too", async () => {
  renderChat("sent", { override: true });
  const trigger = await screen.findByRole("button", { name: "Sends without review" });
  // jsdom applies no media queries: read which label each Tailwind breakpoint shows.
  const labels = [...trigger.querySelectorAll("span")].map((span) => ({
    text: span.textContent,
    classes: span.className.split(/\s+/),
  }));
  const shown = (hiddenBy: string) =>
    labels.filter(({ classes }) => !classes.includes(hiddenBy) && !classes.includes("hidden")).map(({ text }) => text);
  expect(shown("max-sm:hidden")).toEqual(["No review"]);
  expect(shown("sm:hidden")).toEqual(["Sends without review"]);
});

it("changes this chat's send mode from the chip's menu", async () => {
  const { writes } = renderChat("sent", { override: true });
  const trigger = await screen.findByRole("button", { name: "Sends without review" });
  fireEvent.keyDown(trigger, { key: "Enter" });
  const menu = await screen.findByRole("menu");
  const choice = (name: string) => within(menu).getByRole("menuitemradio", { name });
  expect(choice("Organization default · Review before sending").getAttribute("aria-checked")).toBe("false");
  expect(choice("Send without review").getAttribute("aria-checked")).toBe("true");

  fireEvent.click(choice("Review before sending"));
  await waitFor(() => {
    expect(chip()).toBeNull();
  });
  expect(writes).toEqual([
    { route: `PATCH /api/conversations/${CONVERSATION_ID}`, body: { send_without_confirmation: false } },
  ]);
});

it("changes it from the + menu's Harmony connector too", async () => {
  const { writes } = renderChat("sent");
  fireEvent.keyDown(await screen.findByRole("button", { name: "Add files, connectors and more" }), { key: "Enter" });
  fireEvent.keyDown(await screen.findByRole("menuitem", { name: "Connectors" }), { key: "ArrowRight" });
  fireEvent.keyDown(await screen.findByRole("menuitem", { name: "Harmony" }), { key: "ArrowRight" });
  const organization = await screen.findByRole("menuitemradio", { name: "Organization default · Review before sending" });
  expect(organization.getAttribute("aria-checked")).toBe("true");

  fireEvent.click(screen.getByRole("menuitemradio", { name: "Send without review" }));
  expect(await screen.findByRole("button", { name: "Sends without review" })).toBeTruthy();
  expect(writes).toEqual([
    { route: `PATCH /api/conversations/${CONVERSATION_ID}`, body: { send_without_confirmation: true } },
  ]);
});

/** The new-chat screen, with Henderson's default set to `orgDefault`. */
function renderNewChat(orgDefault: boolean) {
  return renderRoutes(
    { ...chatRoutes("drafted"), "GET /api/orgs/{slug}/harmony": { ...hendersonSettings, send_without_confirmation: orgDefault } },
    { element: <NewChatPage />, path: "/", url: "/" },
  );
}

async function sendFirstMessage(text: string): Promise<void> {
  const box = await composer();
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter" });
  await screen.findByText("Elsewhere");
}

it("starts a chat with the send mode the member picked over the organization's default", async () => {
  const { writes } = renderNewChat(true);
  const trigger = await screen.findByRole("button", { name: "Sends without review" });
  fireEvent.keyDown(trigger, { key: "Enter" });
  expect(
    (await screen.findByRole("menuitemradio", { name: "Organization default · Send without review" })).getAttribute(
      "aria-checked",
    ),
  ).toBe("true");
  fireEvent.click(screen.getByRole("menuitemradio", { name: "Review before sending" }));
  await waitFor(() => {
    expect(chip()).toBeNull();
  });

  await sendFirstMessage("Find XRD for 12 powders.");
  expect(writes).toEqual([
    { route: "POST /api/conversations", body: { text: "Find XRD for 12 powders.", send_without_confirmation: false } },
  ]);
});

it("leaves a new chat on the organization's default unless the member picks one", async () => {
  const { writes } = renderNewChat(true);
  await screen.findByRole("button", { name: "Sends without review" });
  await sendFirstMessage("Find XRD for 12 powders.");
  expect(writes).toEqual([{ route: "POST /api/conversations", body: { text: "Find XRD for 12 powders." } }]);
});

it("follows the organization's default as an admin changes it, while the chat has no override", async () => {
  const options: ChatOptions = { orgDefault: false };
  const { sources } = renderRoutes(chatStory("sent", options).routes);
  await composer();
  await waitFor(() => {
    expect(sources.opened).toHaveLength(1);
  });
  act(() => {
    sources.latest.accept();
  });
  expect(chip()).toBeNull();

  const policyChanged = (orgDefault: boolean) => {
    options.orgDefault = orgDefault;
    act(() => {
      sources.latest.send(snapshotEnvelope("rep_policy", orgs.henderson.id, { send_without_confirmation: orgDefault }));
    });
  };
  policyChanged(true);
  expect(await screen.findByRole("button", { name: "Sends without review" })).toBeTruthy();
  policyChanged(false);
  await waitFor(() => {
    expect(chip()).toBeNull();
  });
});

it("holds a new chat's first message until the organization's default is known", async () => {
  let release = () => {};
  const loaded = new Promise<void>((resolve) => {
    release = resolve;
  });
  const { writes } = renderRoutes(
    {
      ...chatRoutes("drafted"),
      "GET /api/orgs/{slug}/harmony": async () => {
        await loaded;
        return { ...hendersonSettings, send_without_confirmation: true };
      },
    },
    { element: <NewChatPage />, path: "/", url: "/" },
  );
  const box = await composer();
  fireEvent.change(box, { target: { value: "Find XRD for 12 powders." } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled).toBe(true);
  expect(writes).toEqual([]);

  release();
  expect(await screen.findByRole("button", { name: "Sends without review" })).toBeTruthy();
  await sendFirstMessage("Find XRD for 12 powders.");
  expect(writes).toEqual([{ route: "POST /api/conversations", body: { text: "Find XRD for 12 powders." } }]);
});

it("holds a new chat's first message while the organization's default won't load, until it does", async () => {
  let failing = true;
  const { writes } = renderRoutes(
    {
      ...chatRoutes("drafted"),
      "GET /api/orgs/{slug}/harmony": () =>
        failing
          ? problem(403, "forbidden", "Not allowed.")
          : { ...hendersonSettings, send_without_confirmation: true },
    },
    { element: <NewChatPage />, path: "/", url: "/" },
  );
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("Couldn't load how your organization sends requests");
  const box = await composer();
  fireEvent.change(box, { target: { value: "Find XRD for 12 powders." } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled).toBe(true);
  expect(chip()).toBeNull();
  expect(writes).toEqual([]);

  // The menu doesn't claim a default it doesn't know.
  fireEvent.keyDown(screen.getByRole("button", { name: "Add files, connectors and more" }), { key: "Enter" });
  fireEvent.keyDown(await screen.findByRole("menuitem", { name: "Connectors" }), { key: "ArrowRight" });
  fireEvent.keyDown(await screen.findByRole("menuitem", { name: "Harmony" }), { key: "ArrowRight" });
  expect((await screen.findByRole("menuitemradio", { name: "Organization default" })).getAttribute("aria-checked")).toBe(
    "true",
  );
  fireEvent.keyDown(screen.getByRole("menuitemradio", { name: "Organization default" }), { key: "Escape" });

  failing = false;
  fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
  expect(await screen.findByRole("button", { name: "Sends without review" })).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  await sendFirstMessage("Find XRD for 12 powders.");
  expect(writes).toEqual([{ route: "POST /api/conversations", body: { text: "Find XRD for 12 powders." } }]);
});

it("starts a chat the member set a mode for though the organization's default won't load", async () => {
  const { writes } = renderRoutes(
    { ...chatRoutes("drafted"), "GET /api/orgs/{slug}/harmony": () => problem(403, "forbidden", "Not allowed.") },
    { element: <NewChatPage />, path: "/", url: "/" },
  );
  await screen.findByRole("alert");
  fireEvent.keyDown(screen.getByRole("button", { name: "Add files, connectors and more" }), { key: "Enter" });
  fireEvent.keyDown(await screen.findByRole("menuitem", { name: "Connectors" }), { key: "ArrowRight" });
  fireEvent.keyDown(await screen.findByRole("menuitem", { name: "Harmony" }), { key: "ArrowRight" });
  fireEvent.click(await screen.findByRole("menuitemradio", { name: "Review before sending" }));
  await waitFor(() => {
    expect(screen.queryByRole("alert")).toBeNull();
  });

  await sendFirstMessage("Find XRD for 12 powders.");
  expect(writes).toEqual([
    { route: "POST /api/conversations", body: { text: "Find XRD for 12 powders.", send_without_confirmation: false } },
  ]);
});

// --- The model label (LIVE_DEMO_PLAN 3.13 E6) ------------------------------------------

it("labels the composer with the model the session runs, marked Adaptive when it is", async () => {
  renderChat("sent");
  expect((await screen.findByRole("button", { name: "Opus 5.5 · Adaptive" })).textContent).toBe("Opus 5.5 · Adaptive");
  cleanup();

  const scripted = { ...sessionAs(members.alex), internal_model: { id: "scripted", label: "Scripted", adaptive: false } };
  renderRoutes({ ...chatRoutes("sent"), "GET /api/session": scripted });
  expect(await screen.findByRole("button", { name: "Scripted" })).toBeTruthy();
  cleanup();

  const haiku = { ...sessionAs(members.alex), internal_model: { id: "claude-haiku-5-5", label: "Haiku 5.5", adaptive: true } };
  renderRoutes({ ...chatRoutes("sent"), "GET /api/session": haiku });
  expect(await screen.findByRole("button", { name: "Haiku 5.5 · Adaptive" })).toBeTruthy();
});

// --- Ticks and asks (LIVE_DEMO_PLAN 3.13 E2, E4) ---------------------------------------

/** Alex's chat with the card waiting, its recipients and brief replaced. */
function renderCard(patch: Partial<OutreachCardItem>) {
  const detail = conversationAt(initialState("drafted"));
  const items = detail.items.map((item) => (item.type === "outreach_card" ? { ...item, ...patch } : item));
  return renderRoutes({ ...chatRoutes("drafted"), "GET /api/conversations/{conversation_id}": () => ({ ...detail, items }) });
}

const [NORTHLAKE, RIDGELINE] = cardRecipients as [CardRecipient, CardRecipient, CardRecipient];

it("starts a row Claude didn't suggest unticked, whatever its fit", async () => {
  renderCard({ recipients: cardRecipients.map((row) => (row === RIDGELINE ? { ...row, suggested: false } : row)) });
  const list = await screen.findByRole("listbox", { name: "Send this request to 1 organization?" });
  fireEvent.click(within(list).getByRole("option", { name: /Send to a subset/ }));
  expect(screen.getByRole<HTMLInputElement>("checkbox", { name: /Northlake/ }).checked).toBe(true);
  expect(screen.getByRole<HTMLInputElement>("checkbox", { name: /Ridgeline Fabrication/ }).checked).toBe(false);
});

it("titles the card, and the composer's strip, by the ticked set", async () => {
  renderChat("drafted");
  fireEvent.click(within(await askCard()).getByRole("option", { name: /Send to a subset/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Calder Computational/ }));
  expect(screen.getByRole("heading", { name: "Send this request to 3 organizations?" })).toBeTruthy();
  fireEvent.click(screen.getByRole("checkbox", { name: /Northlake/ }));
  expect(screen.getByRole("heading", { name: "Send this request to 2 organizations?" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
  expect(await screen.findByRole("button", { name: /Send this request to 2 organizations\?\s*Review/ })).toBeTruthy();
});

it("sends to every row once all are ticked, a near miss included", async () => {
  const { writes } = renderChat("drafted");
  fireEvent.click(within(await askCard()).getByRole("option", { name: /Send to a subset/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /Calder Computational/ }));
  fireEvent.click(screen.getByRole("button", { name: "Send to 3 organizations" }));
  await waitFor(() => {
    expect(writes).toEqual([
      { route: APPROVE, body: { recipients: [orgs.northlake.id, orgs.ridgeline.id, orgs.calder.id] } },
    ]);
  });
});

it("shows each organization's subject and ask in the subset view", async () => {
  renderChat("drafted");
  const card = await screen.findByRole("region", { name: "Send this request to 2 organizations?" });
  fireEvent.click(within(card).getByRole("option", { name: /Send to a subset/ }));
  for (const row of cardRecipients) {
    expect(within(card).getByText(row.title ?? "")).toBeTruthy();
    expect(within(card).getByText(row.ask ?? "")).toBeTruthy();
  }
});

it("sends an edited ask in asks, for that organization only", async () => {
  const { writes } = renderChat("drafted");
  fireEvent.click(within(await askCard()).getByRole("option", { name: /Edit brief/ }));
  // Only the ticked organizations have an ask to edit.
  expect(screen.queryByRole("textbox", { name: "Ask for Calder Computational" })).toBeNull();
  const ask = screen.getByRole("textbox", { name: "Ask for Northlake Characterization Core" });
  expect(ask).toHaveProperty("value", NORTHLAKE.ask);
  fireEvent.change(ask, { target: { value: "Quote XRD only, under $900." } });
  fireEvent.click(screen.getByRole("button", { name: "Send with changes" }));
  await waitFor(() => {
    expect(writes).toEqual([
      {
        route: APPROVE,
        body: { recipients: [orgs.northlake.id, orgs.ridgeline.id], asks: { [orgs.northlake.id]: "Quote XRD only, under $900." } },
      },
    ]);
  });
});

// --- View more (LIVE_DEMO_PLAN 3.11) ---------------------------------------------------

const viewMore = (within_: HTMLElement) => within(within_).queryAllByRole("button", { name: "View more" });

it("clamps a long brief, and long asks, behind View more in the choose and subset views", async () => {
  simulateLayout();
  renderChat("drafted", { long: true });
  const card = await screen.findByRole("region", { name: "Send this request to 2 organizations?" });
  // The brief; the names under the first option fit.
  expect(viewMore(card)).toHaveLength(1);

  fireEvent.click(within(card).getByRole("option", { name: /Send to a subset/ }));
  // The brief, and each organization's ask.
  expect(viewMore(card)).toHaveLength(4);
  fireEvent.click(viewMore(card)[0] ?? card);
  expect(within(card).getByRole("button", { name: "View less" }).getAttribute("aria-expanded")).toBe("true");
});

it("grows an edit box past seven lines to its whole text on View more, with nothing left to scroll", async () => {
  simulateLayout();
  renderChat("drafted", { long: true });
  fireEvent.click(within(await askCard()).getByRole("option", { name: /Edit brief/ }));
  const toggleFor = (box: HTMLElement) =>
    screen
      .queryAllByRole("button", { name: /^View (more|less)$/ })
      .find((button) => button.getAttribute("aria-controls") === box.id);

  const brief = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Brief" });
  expect(brief.clientHeight).toBeLessThan(brief.scrollHeight);
  const more = toggleFor(brief);
  if (more === undefined) throw new Error("no View more for the brief");
  fireEvent.click(more);
  expect(brief.clientHeight).toBeGreaterThanOrEqual(brief.scrollHeight);
  expect(more.textContent).toBe("View less");

  // Six lines fit in the seven a box shows, so the ask has nothing to unfold.
  const ask = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Ask for Northlake Characterization Core" });
  expect(ask.clientHeight).toBeGreaterThanOrEqual(ask.scrollHeight);
  expect(toggleFor(ask)).toBeUndefined();
});

it("shows no View more where the text fits", async () => {
  simulateLayout();
  renderCard({
    brief: "Quote XRD on 12 powders by Nov 6.",
    recipients: cardRecipients.map((row) => ({ ...row, ask: "Quote it." })),
  });
  const card = await screen.findByRole("region", { name: "Send this request to 2 organizations?" });
  expect(viewMore(card)).toEqual([]);
  fireEvent.click(within(card).getByRole("option", { name: /Send to a subset/ }));
  expect(viewMore(card)).toEqual([]);
  fireEvent.click(within(card).getByRole("button", { name: "Back" }));
  fireEvent.click(within(await askCard()).getByRole("option", { name: /Edit brief/ }));
  expect(viewMore(card)).toEqual([]);
});

it("clamps a long list of names under the first option, which View more only unfolds", async () => {
  simulateLayout();
  const many = Array.from({ length: 8 }, (_, index): CardRecipient => ({
    ...NORTHLAKE,
    org: {
      id: `0b1d7a3e-0000-4000-8000-0000000009${String(index).padStart(2, "0")}`,
      slug: `lab-${index}`,
      name: `Materials Laboratory Number ${index}`,
    },
  }));
  const { writes } = renderCard({ brief: "Quote XRD on 12 powders by Nov 6.", recipients: many });
  const card = await screen.findByRole("region", { name: "Send this request to 8 organizations?" });
  const first = within(card).getByRole("option", { name: /Send to all 8/ });
  fireEvent.click(within(first).getByRole("button", { name: "View more" }));
  expect(within(first).getByRole("button", { name: "View less" })).toBeTruthy();
  expect(writes).toEqual([]);
});
