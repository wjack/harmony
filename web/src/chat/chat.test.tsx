// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import type { EntitySnapshot, EventEnvelope } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import { fetchStreamSnapshot } from "../app/snapshot";
import { fixtureFetch, problem, type FixtureRoutes } from "../fixtures";
import {
  chatDeltaEnvelope,
  chatRoutes,
  chatStory,
  conversationAt,
  CONVERSATION_ID,
  initialState,
  NORTHLAKE_GO_AHEAD_ID,
  NORTHLAKE_THREAD_ID,
  OUTREACH_CARD_ID,
  RIDGELINE_GO_AHEAD_ID,
  SEAL_QUESTION_ID,
  TURN,
  type ChatStage,
} from "../fixtures/chat";
import { FIXTURE_NOW, orgs, shellRoutes, WORLD_ID } from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { EventSources } from "../realtime/testing";
import { ARRIVAL_PAUSE_MS } from "./cards/AskUserCard";
import { Markdown } from "./Markdown";
import { optimisticStore } from "./optimistic";
import { ChatPage } from "./pages";
import { TURN_FAILED_TEXT } from "./Transcript";

interface Call {
  route: string;
  body: unknown;
}

async function bodyOf(request: Request): Promise<unknown> {
  const text = await request.clone().text();
  return text === "" ? undefined : (JSON.parse(text) as unknown);
}

/** `/chat/:id` over the story's fixtures at `stage`, recording every write. */
function renderChat(stage: ChatStage) {
  return renderRoutes(chatRoutes(stage));
}

function renderRoutes(routes: FixtureRoutes) {
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
      { path: "/chat/:id", element: <ChatPage /> },
      { path: "*", element: <p>Elsewhere</p> },
    ],
    { initialEntries: [`/chat/${CONVERSATION_ID}`] },
  );
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { writes, sources, dispatcher };
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
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
  // jsdom doesn't scroll.
  Element.prototype.scrollTo = () => undefined;
});

afterEach(() => {
  cleanup();
  optimisticStore.setState({ byConversation: {} });
});

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

  const list = await askCard();
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
  // As the representative's prompt has it (prompts/side_requester.md): what the
  // recipients need goes out, the rest stays home unless the brief says otherwise. A
  // request, not a guarantee.
  const hint = brief.getAttribute("aria-describedby");
  expect(hint === null ? "" : document.getElementById(hint)?.textContent).toMatch(
    /tells the recipients what they need to answer, and it is asked to keep the rest, such as a budget, to itself/,
  );
  // The recorded story doesn't follow an edited brief, and the panel says so.
  expect(screen.getByText(/^Scripted demo: Claude replays the recorded story/)).toBeTruthy();
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
  const failed = { ...conversationAt(initialState("closed")), turn_failed: true };
  renderRoutes({ ...chatRoutes("closed"), "GET /api/conversations/{conversation_id}": () => failed });
  expect((await screen.findByText(TURN_FAILED_TEXT)).getAttribute("role")).toBe("alert");
  cleanup();

  renderRoutes({
    ...chatRoutes("closed"),
    "GET /api/conversations/{conversation_id}": () => ({ ...failed, turn_in_flight: true }),
  });
  await composer();
  expect(screen.queryByText(TURN_FAILED_TEXT)).toBeNull();
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
