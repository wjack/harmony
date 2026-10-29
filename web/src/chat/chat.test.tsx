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
  brief,
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
  REDRAFT_CARD_ID,
  REDRAFT_MESSAGE,
  REQUEST_TITLE,
  requestAt,
  RIDGELINE_GO_AHEAD_ID,
  TUBE_QUESTION_ID,
  SUMMARIES,
  RUNNING_SEARCH_LABEL,
  SENT_REPLY,
  TURN,
  type ChatOptions,
  type ChatStage,
} from "../fixtures/chat";
import { hendersonSettings } from "../fixtures/customize";
import { alexInbox, budgets, FIXTURE_NOW, members, orgs, sessionAs, shellRoutes, WORLD_ID } from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { SidebarRequests } from "../requests/SidebarRequests";
import { EventSources } from "../realtime/testing";
import { ARRIVAL_PAUSE_MS, OutreachRecord } from "./cards/OutreachCard";
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

/** The waiting card: a region named for what it is and the request's title. */
const outreach = () => screen.findByRole("region", { name: `Harmony Network Request ${REQUEST_TITLE}` });
const composer = () => screen.findByRole("textbox", { name: "Write your message to Claude" });
const approveIn = (card: HTMLElement) => within(card).getByRole("button", { name: "Approve and send" });
const tick = (card: HTMLElement, org: string) => within(card).getByRole<HTMLInputElement>("checkbox", { name: org });
const APPROVE = `POST /api/cards/${OUTREACH_CARD_ID}/approve`;
const REJECT = `POST /api/cards/${OUTREACH_CARD_ID}/reject`;

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
  // Measures once, on observe.
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

it("shows the request inline, in full, and sends it to the organizations Claude picked", async () => {
  const { writes } = renderChat("drafted");
  const card = await outreach();
  expect(within(card).getByText("Claude screened 14 organizations and picked 2.")).toBeTruthy();
  // Every part reads in full: no clamps, no panels.
  expect(within(card).getByRole("textbox", { name: "Brief" })).toHaveProperty("value", brief);
  for (const row of cardRecipients.slice(0, 2)) {
    expect(within(card).getByText(row.title ?? "")).toBeTruthy();
    expect(within(card).getByRole("textbox", { name: `Ask for ${row.org.name}` })).toHaveProperty("value", row.ask);
  }
  expect(within(card).queryByRole("button", { name: /more/i })).toBeNull();
  // The box at the bottom stays, asking what Claude should change.
  expect(await composer()).toHaveProperty("placeholder", "What should Claude do differently?");
  expect(within(card).getByText("Starts a conversation with Northlake Characterization Core and Ridgeline Fabrication · ⌘↵")).toBeTruthy();

  fireEvent.click(approveIn(card));

  expect(await screen.findByText(SENT_REPLY)).toBeTruthy();
  expect(writes).toEqual([{ route: APPROVE, body: { recipients: [orgs.northlake.id, orgs.ridgeline.id] } }]);
  // The card folds to a one-line record; the request's live status loads under it.
  expect(await screen.findByText("Sent")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Approve and send" })).toBeNull();
  expect(await screen.findByText("Open in Requests")).toBeTruthy();
  expect(await composer()).toHaveProperty("placeholder", "Reply…");
});

it("sends to the ticked organizations, a not-a-fit one unticked until chosen", async () => {
  const { writes } = renderChat("drafted");
  const card = await outreach();
  expect(tick(card, "Calder Computational").checked).toBe(false);
  fireEvent.click(tick(card, "Ridgeline Fabrication"));
  expect(within(card).getByText("Left out: Ridgeline Fabrication gets nothing from this request. Tick it to add it back.")).toBeTruthy();
  expect(within(card).queryByRole("textbox", { name: "Ask for Ridgeline Fabrication" })).toBeNull();
  expect(within(card).getByText("Claude screened 14 organizations and picked 2; you've chosen 1.")).toBeTruthy();
  expect(within(card).getByText("Starts a conversation with Northlake Characterization Core only · ⌘↵")).toBeTruthy();
  fireEvent.click(approveIn(card));

  await waitFor(() => {
    expect(writes).toEqual([{ route: APPROVE, body: { recipients: [orgs.northlake.id] } }]);
  });
});

it("holds Approve with nothing ticked, or a part cleared", async () => {
  renderChat("drafted");
  const card = await outreach();
  fireEvent.click(tick(card, "Northlake Characterization Core"));
  fireEvent.click(tick(card, "Ridgeline Fabrication"));
  expect(approveIn(card)).toHaveProperty("disabled", true);
  expect(within(card).getByText("Tick at least one organization")).toBeTruthy();

  fireEvent.click(tick(card, "Northlake Characterization Core"));
  fireEvent.change(within(card).getByRole("textbox", { name: "Brief" }), { target: { value: "  " } });
  expect(approveIn(card)).toHaveProperty("disabled", true);
  expect(within(card).getByText("The brief and each ask need some text")).toBeTruthy();
});

it("declines with an optional reason that only Claude in this chat sees", async () => {
  const { writes } = renderChat("drafted");
  const card = await outreach();
  fireEvent.click(within(card).getByRole("button", { name: "Decline" }));
  const step = within(card).getByRole("region", { name: "Don't send this request" });
  expect(within(step).getByText(/Only Claude in this chat sees this\./)).toBeTruthy();
  // Cancel puts the buttons back, every tick kept.
  fireEvent.click(tick(card, "Ridgeline Fabrication"));
  fireEvent.click(within(step).getByRole("button", { name: "Cancel" }));
  expect(tick(card, "Ridgeline Fabrication").checked).toBe(false);

  fireEvent.click(within(card).getByRole("button", { name: "Decline" }));
  const reason = within(card).getByRole("textbox", { name: "Reason" });
  fireEvent.change(reason, { target: { value: "Not until the PI confirms the budget." } });
  fireEvent.keyDown(reason, { key: "Enter", metaKey: true });

  expect(await screen.findByText("Declined")).toBeTruthy();
  expect(writes).toEqual([{ route: REJECT, body: { feedback: "Not until the PI confirms the budget." } }]);
  expect(screen.queryByRole("button", { name: "Approve and send" })).toBeNull();
});

it("declines with no reason as an empty body", async () => {
  const { writes } = renderChat("drafted");
  const card = await outreach();
  fireEvent.click(within(card).getByRole("button", { name: "Decline" }));
  const step = within(card).getByRole("region", { name: "Don't send this request" });
  fireEvent.click(within(step).getByRole("button", { name: "Decline" }));
  await screen.findByText("Declined");
  expect(writes).toEqual([{ route: REJECT, body: {} }]);
});

it("approves on ⌘↵ inside the card, and nowhere else", async () => {
  const { writes } = renderChat("drafted");
  const card = await outreach();
  fireEvent.keyDown(await composer(), { key: "Enter", metaKey: true });
  fireEvent.keyDown(document.body, { key: "Enter", metaKey: true });
  expect(writes).toEqual([]);

  fireEvent.keyDown(within(card).getByRole("textbox", { name: "Brief" }), { key: "Enter", metaKey: true });
  await waitFor(() => {
    expect(writes).toEqual([{ route: APPROVE, body: { recipients: [orgs.northlake.id, orgs.ridgeline.id] } }]);
  });
});

it("folds to its title and who it goes to, keeping every tick", async () => {
  renderChat("drafted");
  const card = await outreach();
  fireEvent.click(tick(card, "Ridgeline Fabrication"));
  fireEvent.click(within(card).getByRole("button", { name: "Collapse" }));
  expect(within(card).getByText("Waiting for you")).toBeTruthy();
  expect(within(card).getByText("To Northlake Characterization Core")).toBeTruthy();
  // Approving needs the card open.
  expect(within(card).queryByRole("button", { name: "Approve and send" })).toBeNull();

  fireEvent.click(within(card).getByRole("button", { name: "Show" }));
  expect(tick(card, "Ridgeline Fabrication").checked).toBe(false);
});

it("shows Review while the card is out of view, which opens it and scrolls to it", async () => {
  const observers: { callback: IntersectionObserverCallback; target: Element | null }[] = [];
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      readonly #entry: (typeof observers)[number];
      constructor(callback: IntersectionObserverCallback) {
        this.#entry = { callback, target: null };
        observers.push(this.#entry);
      }
      observe(target: Element): void {
        this.#entry.target = target;
      }
      disconnect(): void {}
    },
  );
  const scrolled = vi.fn();
  Element.prototype.scrollTo = scrolled;
  renderChat("drafted");
  const card = await outreach();
  fireEvent.click(within(card).getByRole("button", { name: "Collapse" }));
  expect(screen.queryByRole("button", { name: /Review/ })).toBeNull();

  act(() => {
    for (const { callback, target } of observers) {
      if (target !== null) callback([{ isIntersecting: false, target } as IntersectionObserverEntry], {} as IntersectionObserver);
    }
  });
  fireEvent.click(await screen.findByRole("button", { name: /Send this request to 2 organizations\?\s*Review/ }));
  expect(within(card).getByRole("button", { name: "Approve and send" })).toBeTruthy();
  expect(scrolled).toHaveBeenCalledWith(expect.objectContaining({ behavior: "smooth" }));
});

it("answers a counterparty's question inline", async () => {
  const { writes } = renderChat("question");
  const answer = await screen.findByRole("textbox", { name: "Your answer about Ridgeline Fabrication" });
  fireEvent.change(answer, { target: { value: "Carbon fiber, please." } });
  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));

  await screen.findByText("Answered");
  expect(writes).toEqual([
    {
      route: `POST /api/escalations/${TUBE_QUESTION_ID}/resolve`,
      body: { action: "approve", text: "Carbon fiber, please.", also_for: [] },
    },
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
  await answerAbout("Ridgeline Fabrication", "Yes, with the carbon-fiber tube.");

  expect(await screen.findByText(/inside your \$2,000 budget/)).toBeTruthy();
  expect(writes).toEqual([
    {
      route: `POST /api/escalations/${NORTHLAKE_GO_AHEAD_ID}/resolve`,
      body: { action: "approve", text: "Yes, go ahead.", also_for: [] },
    },
    {
      route: `POST /api/escalations/${RIDGELINE_GO_AHEAD_ID}/resolve`,
      body: { action: "approve", text: "Yes, with the carbon-fiber tube.", also_for: [] },
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
  await screen.findByText(RUNNING_SEARCH_LABEL);
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

it("sends edited wording with only what changed, and says so under the buttons and in the record", async () => {
  const { writes } = renderChat("drafted");
  const card = await outreach();
  const briefBox = within(card).getByRole<HTMLTextAreaElement>("textbox", { name: "Brief" });
  expect(within(card).getByText("Click the brief or an ask to change the wording")).toBeTruthy();
  fireEvent.change(briefBox, { target: { value: `${briefBox.value}\n\nResults by Nov 4 if you can.  ` } });
  const ask = within(card).getByRole("textbox", { name: "Ask for Northlake Characterization Core" });
  fireEvent.change(ask, { target: { value: "Quote the CT only, under $900." } });
  // Each changed part says so, with Undo; Calder isn't ticked, so it has no ask to edit.
  expect(within(card).getAllByText("· edited by you")).toHaveLength(2);
  expect(within(card).queryByRole("textbox", { name: "Ask for Calder Computational" })).toBeNull();
  expect(
    within(card).getByText("Starts a conversation with Northlake Characterization Core and Ridgeline Fabrication, with your edits · ⌘↵"),
  ).toBeTruthy();
  fireEvent.click(approveIn(card));

  await waitFor(() => {
    expect(writes).toHaveLength(1);
  });
  expect(writes[0]).toEqual({
    route: APPROVE,
    body: {
      recipients: [orgs.northlake.id, orgs.ridgeline.id],
      brief: expect.stringMatching(/^CT scans and compression tests[\s\S]*Results by Nov 4 if you can\.$/) as unknown,
      asks: { [orgs.northlake.id]: "Quote the CT only, under $900." },
    },
  });
  // The card folds to its record, which still says what went out wasn't Claude's draft.
  expect(
    await screen.findByText(/^Sent to Northlake Characterization Core and Ridgeline Fabrication, with your edits/),
  ).toBeTruthy();
});

it("puts Claude's wording back on Undo my edits", async () => {
  renderChat("drafted");
  const card = await outreach();
  const ask = within(card).getByRole("textbox", { name: "Ask for Ridgeline Fabrication" });
  fireEvent.change(ask, { target: { value: "Quote it." } });
  fireEvent.click(within(card).getByRole("button", { name: "Undo my edits" }));
  expect(ask).toHaveProperty("value", RIDGELINE.ask);
  expect(within(card).queryByText("· edited by you")).toBeNull();
});

it("takes a message sent while the card waits as a request to redraft it", async () => {
  const { writes } = renderChat("drafted");
  await outreach();
  const box = await composer();
  fireEvent.change(box, { target: { value: REDRAFT_MESSAGE } });
  fireEvent.keyDown(box, { key: "Enter" });

  // The old card folds into one quiet row, the message reads as Alex's, and the redraft lands at the end.
  expect(await screen.findByText("Earlier version")).toBeTruthy();
  expect(writes).toEqual([{ route: `POST /api/conversations/${CONVERSATION_ID}/messages`, body: { text: REDRAFT_MESSAGE } }]);
  expect(screen.getByText(REDRAFT_MESSAGE)).toBeTruthy();
  const card = await outreach();
  expect(within(card).getAllByText("· rewritten by Claude").length).toBeGreaterThan(0);
});

it("pages a rewritten part back to Claude's earlier wording, which goes as an edit", async () => {
  const { writes } = renderChat("drafted", { rejected: { with: "message", text: REDRAFT_MESSAGE } });
  const card = await outreach();
  const briefBox = within(card).getByRole<HTMLTextAreaElement>("textbox", { name: "Brief" });
  expect(briefBox.value).toContain("engraved");
  const [earlier] = within(card).getAllByRole("button", { name: "Earlier version" });
  if (earlier === undefined) throw new Error("no pager on the brief");
  fireEvent.click(earlier);
  expect(briefBox.value).not.toContain("engraved");
  fireEvent.click(approveIn(card));

  await waitFor(() => {
    expect(writes).toHaveLength(1);
  });
  expect(writes[0]?.route).toBe(`POST /api/cards/${REDRAFT_CARD_ID}/approve`);
  expect(writes[0]?.body).toMatchObject({ brief: expect.not.stringContaining("engraved") as unknown });
});

it("shows a card that arrives mid-draft at the end of the chat, leaving the draft in the box", async () => {
  const story = chatStory("working");
  const { writes, sources } = renderRoutes(story.routes);
  fireEvent.change(await composer(), { target: { value: "Also ask Calder" } });
  story.advance("drafted");
  await announce(sources, "conversation", CONVERSATION_ID);

  await outreach();
  expect(await composer()).toHaveProperty("value", "Also ask Calder");
  expect(writes).toEqual([]);
});

it("holds Approve on a card that just arrived, for a moment", async () => {
  const story = chatStory("working");
  const { writes, sources } = renderRoutes(story.routes);
  await composer();
  story.advance("drafted");
  await announce(sources, "conversation", CONVERSATION_ID);

  const card = await outreach();
  expect(approveIn(card)).toHaveProperty("disabled", true);
  fireEvent.click(approveIn(card));
  await act(() => new Promise((resolve) => setTimeout(resolve, ARRIVAL_PAUSE_MS + 50)));
  expect(writes).toEqual([]);

  fireEvent.click(approveIn(card));
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
    target: { value: "Carbon fiber, please." },
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

describe("a message Claude declined", () => {
  const MESSAGE = "Also ask Northlake to test two of the coupons to failure.";
  const RETRY = `POST /api/conversations/${CONVERSATION_ID}/retry`;
  const declined = () => screen.findByRole("group", { name: "Message Claude declined" });

  it("says so under the message, and Retry sends it again as a new turn", async () => {
    const { writes } = renderChat("sent", { refused: { text: MESSAGE, category: "bio" } });
    const turn = await declined();
    expect(within(turn).getByText(MESSAGE)).toBeTruthy();
    expect(within(turn).getByText("Claude declined to respond to this (biosecurity).")).toBeTruthy();
    // The note stands in place of the line under the transcript.
    expect(screen.queryByRole("alert")).toBeNull();

    fireEvent.click(within(turn).getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(screen.queryByRole("group", { name: "Message Claude declined" })).toBeNull();
    });
    expect(writes).toEqual([{ route: RETRY, body: {} }]);
    // Sent again, the message shows once.
    expect(screen.getAllByText(MESSAGE)).toHaveLength(1);
  });

  it("opens the message to reword with Edit, and sends the new wording, or nothing on Cancel", async () => {
    const { writes } = renderChat("sent", { refused: { text: MESSAGE, category: "bio" } });
    const turn = await declined();
    fireEvent.click(within(turn).getByRole("button", { name: "Edit" }));
    const field = within(turn).getByRole<HTMLTextAreaElement>("textbox", { name: "Your message" });
    expect(field.value).toBe(MESSAGE);
    expect(document.activeElement).toBe(field);
    fireEvent.click(within(turn).getByRole("button", { name: "Cancel" }));
    expect(within(turn).queryByRole("textbox", { name: "Your message" })).toBeNull();

    fireEvent.click(within(turn).getByRole("button", { name: "Edit" }));
    const reworded = within(turn).getByRole<HTMLTextAreaElement>("textbox", { name: "Your message" });
    fireEvent.change(reworded, { target: { value: "  " } });
    expect(within(turn).getByRole<HTMLButtonElement>("button", { name: "Send" }).disabled).toBe(true);
    fireEvent.change(reworded, { target: { value: "Where do the coupons give under compression?" } });
    fireEvent.keyDown(reworded, { key: "Enter" });
    await waitFor(() => {
      expect(screen.queryByRole("group", { name: "Message Claude declined" })).toBeNull();
    });
    expect(screen.getByText("Where do the coupons give under compression?")).toBeTruthy();
    expect(screen.queryByText(MESSAGE)).toBeNull();
    expect(writes).toEqual([{ route: RETRY, body: { text: "Where do the coupons give under compression?" } }]);
  });

  it("keeps the line under the transcript when the turn can't be sent again", async () => {
    const detail = {
      ...conversationAt(initialState("closed")),
      turn_failure: "refused" as const,
      refusal: { turn_id: "0b1d7a3e-0000-4000-8000-0000000008d6", category: "cyber", retryable: false },
    };
    renderRoutes({ ...chatRoutes("closed"), "GET /api/conversations/{conversation_id}": () => detail });
    const line = await screen.findByText("Claude declined to respond to this (cybersecurity). Try rephrasing.");
    expect(line.getAttribute("role")).toBe("alert");
    expect(screen.queryByRole("group", { name: "Message Claude declined" })).toBeNull();
  });
});

it("says a budget stop by what the budget says now", async () => {
  renderRoutes({ ...chatRoutes("sent", { turnFailure: "demo_budget" }), "GET /api/budget": budgets.demoExhausted });
  expect(await screen.findByText("The demo's live-model budget is used up.")).toBeTruthy();
  cleanup();

  // Spent no longer: the next try may well go through.
  renderChat("sent", { turnFailure: "demo_budget" });
  expect(await screen.findByText(failureLine("demo_budget", "ok"))).toBeTruthy();
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
  // Whose it is, from `GET /api/demo/whose`; this id is in no world of the demo.
  expect(await screen.findByText("This chat isn't in this demo")).toBeTruthy();
});

// --- The live demo's failure states (LIVE_DEMO_PLAN 3.5) ---------------------------------

const BLOCKED = [
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

it("enables the composer while the budget is ok, and again once it's back", async () => {
  let budget: (typeof budgets)[keyof typeof budgets] = budgets.demoExhausted;
  const { writes, queryClient } = renderRoutes({ ...chatRoutes("sent"), "GET /api/budget": () => budget });
  await screen.findByText("The demo's live-model budget is used up");
  expect(await composer()).toHaveProperty("disabled", true);

  budget = budgets.ok;
  await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.budget }));
  await waitFor(() => {
    expect(screen.queryByText("The demo's live-model budget is used up")).toBeNull();
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
  ["demo_exhausted", budgets.demoExhausted, "The demo's live-model budget is used up"],
] as const;

it.each(HOLDS)("holds Approve, Decline and the box on a waiting card while the budget is %s", async (_state, budget, line) => {
  const { writes } = renderRoutes({ ...chatRoutes("drafted"), "GET /api/budget": budget });
  const card = await outreach();
  // One strip, on the card's top edge, not on the box.
  expect(await screen.findAllByText(line)).toHaveLength(1);
  await waitFor(() => {
    expect(approveIn(card)).toHaveProperty("disabled", true);
  });
  expect(within(card).getByRole("button", { name: "Decline" })).toHaveProperty("disabled", true);
  expect(within(card).getByText("Nothing goes out until the budget is back")).toBeTruthy();
  expect(await composer()).toHaveProperty("disabled", true);
  expect(
    screen.getByText("Claude can't redraft until the budget is back. Ticking and changing the wording still work."),
  ).toBeTruthy();
  fireEvent.keyDown(within(card).getByRole("textbox", { name: "Brief" }), { key: "Enter", metaKey: true });

  // Ticking and rewording still work; only what starts Claude's turn holds.
  fireEvent.click(tick(card, "Ridgeline Fabrication"));
  fireEvent.change(within(card).getByRole("textbox", { name: "Brief" }), { target: { value: "A shorter brief." } });
  expect(within(card).getByText("· edited by you")).toBeTruthy();
  expect(writes).toEqual([]);
});

it.each(HOLDS)("holds an inline answer while the budget is %s, and says so", async (_state, budget, line) => {
  const { writes } = renderRoutes({ ...chatRoutes("question"), "GET /api/budget": budget });
  const answer = await screen.findByRole("textbox", { name: "Your answer about Ridgeline Fabrication" });
  const card = screen.getByRole("region", { name: "Your representative asks about Ridgeline Fabrication" });
  expect(await within(card).findByText(line)).toBeTruthy();
  // The answer stays, and nothing goes out.
  fireEvent.change(answer, { target: { value: "Carbon fiber, please." } });
  expect(within(card).getByRole("button", { name: "Send answer" })).toHaveProperty("disabled", true);
  fireEvent.keyDown(answer, { key: "Enter", metaKey: true });
  expect(answer).toHaveProperty("value", "Carbon fiber, please.");
  expect(writes).toEqual([]);
});

it("sends the outreach card once the budget is ok again", async () => {
  let budget: (typeof budgets)[keyof typeof budgets] = budgets.paused;
  const { writes, queryClient } = renderRoutes({ ...chatRoutes("drafted"), "GET /api/budget": () => budget });
  const card = await outreach();
  await screen.findByText("Live models are paused");
  fireEvent.click(approveIn(card));
  expect(writes).toEqual([]);

  budget = budgets.ok;
  await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.budget }));
  await waitFor(() => {
    expect(screen.queryByText("Live models are paused")).toBeNull();
  });
  fireEvent.click(approveIn(card));
  await waitFor(() => {
    expect(writes).toEqual([{ route: APPROVE, body: { recipients: [orgs.northlake.id, orgs.ridgeline.id] } }]);
  });
});

/** The request's status card on its own, as `renderRoutes` serves it. */
async function statusCard(view: ReturnType<typeof requestAt>, routes: FixtureRoutes = {}): Promise<HTMLElement> {
  renderRoutes(routes, { element: <RequestStatusPanel view={view} memberId={members.alex.id} />, path: "/", url: "/" });
  return screen.findByRole("region", { name: "Harmony Network Request" });
}

const REASONS: readonly FailureReason[] = ["demo_budget", "paused", "model_error", "refused"];

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
  const card = await statusCard(requestAt(initialState("sent", { stopReason: "demo_budget" })), {
    "GET /api/budget": budgets.demoExhausted,
  });
  expect(await within(card).findByText(stoppedLine("demo_budget", "demo_exhausted"))).toBeTruthy();
});

it("shows a closed thread's summary when nothing stopped it", async () => {
  const card = await statusCard(requestAt(initialState("closed")));
  expect(within(card).getByText(SUMMARIES.henderson.northlake)).toBeTruthy();
  expect(within(card).getByText(SUMMARIES.henderson.ridgeline)).toBeTruthy();
});

it("lists only the organizations the request went to", async () => {
  const card = await statusCard(requestAt(initialState("sent")));
  expect(within(card).getByText("Sent to 2")).toBeTruthy();
  expect(within(card).queryByText("Calder Computational")).toBeNull();
  expect(within(card).queryByText("Not sent")).toBeNull();
  expect(within(card).queryByText("Screened")).toBeNull();
});

it("names the request's card Harmony Network Request, with the request's title in its body", async () => {
  const card = await statusCard(requestAt(initialState("sent")));
  expect(within(card).getByText(REQUEST_TITLE)).toBeTruthy();
  cleanup();

  render(<OutreachRecord item={outreachCardAt(conversationAt(initialState("sent")))} sentAt={null} />);
  const outreach = screen.getByRole("region", { name: `Harmony Network Request ${REQUEST_TITLE}` });
  expect(within(outreach).getByText("Sent to Northlake Characterization Core and Ridgeline Fabrication")).toBeTruthy();
  // Show what was sent reads it in full: each organization's subject, as it read it.
  fireEvent.click(within(outreach).getByRole("button", { name: "Show what was sent" }));
  expect(within(outreach).getByText("CT holder quote")).toBeTruthy();
  expect(within(outreach).getByText("Not sent to Calder Computational.")).toBeTruthy();
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
] as const)("shows an unreviewed card %s with its line and nothing to decide", (status, state, line) => {
  const item = outreachCardAt(conversationAt(state));
  expect(item.status).toBe(status);
  render(<OutreachRecord item={item} sentAt={null} />);
  const card = screen.getByRole("region", { name: `Harmony Network Request ${REQUEST_TITLE}` });
  expect(within(card).getByText(line)).toBeTruthy();
  expect(within(card).queryByRole("button", { name: "Approve and send" })).toBeNull();
  expect(within(card).queryByRole("button", { name: "Decline" })).toBeNull();
  if (status === "rejected") expect(within(card).getByText("Not sent: Ridgeline wasn't screened as a fit.")).toBeTruthy();
});

it("asks nothing while Claude sends without review", async () => {
  renderChat("drafted", { auto: true, override: true });
  expect(await screen.findByText("Sending…")).toBeTruthy();
  await composer();
  expect(screen.queryByRole("button", { name: "Approve and send" })).toBeNull();
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

  await sendFirstMessage("Find CT for 12 coupons.");
  expect(writes).toEqual([
    { route: "POST /api/conversations", body: { text: "Find CT for 12 coupons.", send_without_confirmation: false } },
  ]);
});

it("leaves a new chat on the organization's default unless the member picks one", async () => {
  const { writes } = renderNewChat(true);
  await screen.findByRole("button", { name: "Sends without review" });
  await sendFirstMessage("Find CT for 12 coupons.");
  expect(writes).toEqual([{ route: "POST /api/conversations", body: { text: "Find CT for 12 coupons." } }]);
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
  fireEvent.change(box, { target: { value: "Find CT for 12 coupons." } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Send message" }).disabled).toBe(true);
  expect(writes).toEqual([]);

  release();
  expect(await screen.findByRole("button", { name: "Sends without review" })).toBeTruthy();
  await sendFirstMessage("Find CT for 12 coupons.");
  expect(writes).toEqual([{ route: "POST /api/conversations", body: { text: "Find CT for 12 coupons." } }]);
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
  fireEvent.change(box, { target: { value: "Find CT for 12 coupons." } });
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
  await sendFirstMessage("Find CT for 12 coupons.");
  expect(writes).toEqual([{ route: "POST /api/conversations", body: { text: "Find CT for 12 coupons." } }]);
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

  await sendFirstMessage("Find CT for 12 coupons.");
  expect(writes).toEqual([
    { route: "POST /api/conversations", body: { text: "Find CT for 12 coupons.", send_without_confirmation: false } },
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

const [, RIDGELINE] = cardRecipients as [CardRecipient, CardRecipient, CardRecipient];

it("starts a row Claude didn't suggest unticked, whatever its fit", async () => {
  renderCard({ recipients: cardRecipients.map((row) => (row === RIDGELINE ? { ...row, suggested: false } : row)) });
  const card = await outreach();
  expect(tick(card, "Northlake Characterization Core").checked).toBe(true);
  expect(tick(card, "Ridgeline Fabrication").checked).toBe(false);
  expect(within(card).getByText("· 1 of 3")).toBeTruthy();
});

it("sends to every row once all are ticked, a near miss included", async () => {
  const { writes } = renderChat("drafted");
  const card = await outreach();
  fireEvent.click(tick(card, "Calder Computational"));
  expect(within(card).getByText("Starts a conversation with each of them · ⌘↵")).toBeTruthy();
  fireEvent.click(approveIn(card));
  await waitFor(() => {
    expect(writes).toEqual([
      { route: APPROVE, body: { recipients: [orgs.northlake.id, orgs.ridgeline.id, orgs.calder.id] } },
    ]);
  });
});

it("changes how the chat sends from the card, for Claude's next request", async () => {
  const { writes } = renderChat("drafted");
  const card = await outreach();
  expect(within(card).getByText(/This chat: review before sending/)).toBeTruthy();
  fireEvent.keyDown(within(card).getByRole("button", { name: "Change how this chat sends requests" }), { key: "Enter" });
  const menu = await screen.findByRole("menu");
  expect(within(menu).getByText("Applies from Claude's next request; this one still waits for you.")).toBeTruthy();
  fireEvent.click(within(menu).getByRole("menuitemradio", { name: "Send without review" }));
  await waitFor(() => {
    expect(writes).toEqual([
      { route: `PATCH /api/conversations/${CONVERSATION_ID}`, body: { send_without_confirmation: true } },
    ]);
  });
  // This card still waits.
  expect(approveIn(card)).toBeTruthy();
});
