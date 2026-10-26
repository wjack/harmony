// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider, useLocation, type RouteObject } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import { queryKeys } from "../api/queryKeys";
import type { Inbox, MemberView, SessionView } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import { fetchStreamSnapshot } from "../app/snapshot";
import { clearAllAlerts } from "../demo/alerts";
import { demoStore } from "../demo/store";
import { fixtureFetch, json, problem, type FixtureRoutes } from "../fixtures";
import {
  alexInbox,
  conversations,
  inbox,
  memberDirectory,
  members,
  sessionAs,
  shellRoutes,
  WORLD_ID,
} from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { EventSources, needsHuman } from "../realtime/testing";
import { AppLayout, RECONNECT_GRACE_MS } from "./AppLayout";
import { OpenEscalation } from "./OpenEscalation";
import { RootLayout } from "./RootLayout";
import { showRail, sidebarStore, toggleSidebar } from "./sidebarStore";
import { toastStore } from "./toasts";

const NEW_WORLD_ID = "0b1d7a3e-5c2f-4e8a-9a51-000000000002";
const RECENT = conversations[0]?.title ?? "";
// Helen's inbox (fixtures/shell.ts): item n is escalation …3e<n> in thread …3b<n>.
const ESCALATION = (n: number) => `0b1d7a3e-0000-4000-8000-0000000003e${n}`;
const THREAD = (n: number) => `0b1d7a3e-0000-4000-8000-0000000003b${n}`;

/** The shell's routes with stand-ins for the areas other tasks own. */
function Page({ name }: { name: string }) {
  const location = useLocation();
  return (
    <p>
      {name} at {location.pathname}
      {location.search}
    </p>
  );
}

const testRoutes: RouteObject[] = [
  {
    element: <RootLayout />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { index: true, element: <Page name="Home" /> },
          { path: "chat/:id", element: <Page name="Chat" /> },
          {
            path: "requests",
            element: (
              <OpenEscalation>
                <Page name="Requests" />
              </OpenEscalation>
            ),
          },
          { path: "requests/:threadId", element: <Page name="Thread" /> },
          { path: "network", element: <Page name="Network" /> },
        ],
      },
    ],
  },
];

/** Opens when the test says so: holds a response to look at the app mid-request. */
class Gate {
  readonly promise: Promise<void>;
  #open: () => void = () => {};

  constructor() {
    this.promise = new Promise((resolve) => {
      this.#open = resolve;
    });
  }

  open(): void {
    this.#open();
  }
}

/** The shell fixtures behind a cursor that moves on act-as and reset, as the API's does. */
class FakeServer {
  session: SessionView | null;
  cursor = 7;
  /** While set, `GET /api/session` waits for it. */
  sessionGate: Gate | null = null;
  /** While true, `GET /api/session` answers 503. */
  down = false;
  /** While set, act-as answers with it instead of switching. */
  actAsFailure: (() => Response) | null = null;
  /** Alex's inbox: empty unless a test gives him a question. */
  alexInbox: Inbox = { ...inbox, needs_you: [] };
  readonly calls: string[] = [];
  readonly #routes: FixtureRoutes;

  constructor(session: SessionView | null) {
    this.session = session;
    const everyone = [...memberDirectory.featured, ...memberDirectory.all];
    this.#routes = {
      ...shellRoutes(),
      "GET /api/session": async () => {
        if (this.down) return problem(503, "unavailable", "Restarting.");
        await this.sessionGate?.promise;
        return this.session ?? problem(401, "no_session", "No session.");
      },
      "POST /api/session/start": () => {
        this.session = sessionAs(members.alex, 1);
        return json(this.session, 200, this.cursor);
      },
      "POST /api/session/act-as": ({ body }) => {
        if (this.actAsFailure !== null) return this.actAsFailure();
        const target = everyone.find((candidate) => candidate.id === (body as { member_id: string }).member_id);
        if (this.session === null || target === undefined) return problem(403, "forbidden", "No.");
        this.cursor += 5; // act-as commits session.member_changed
        this.session = sessionAs(target, this.session.epoch + 1, this.session.world_id);
        return json(this.session, 200, this.cursor);
      },
      // Recents are Alex's; the Requests items need Helen.
      "GET /api/conversations": () => (this.acting?.slug === members.alex.slug ? conversations : []),
      "GET /api/inbox": () =>
        this.acting?.slug === members.helen.slug
          ? inbox
          : this.acting?.slug === members.alex.slug
            ? this.alexInbox
            : { ...inbox, needs_you: [] },
      "POST /api/worlds/reset": () => {
        if (this.session === null) return problem(401, "no_session", "No session.");
        this.cursor = 3; // a new world starts its own log
        // Back at the start of the story, as its member.
        this.session = sessionAs(members.alex, this.session.epoch + 1, NEW_WORLD_ID);
        return json(
          { session: this.session, previous_world_id: WORLD_ID, previous_world_status: "retiring" },
          200,
          this.cursor,
        );
      },
    };
  }

  readonly fetch = (request: Request): Promise<Response> => {
    this.calls.push(`${request.method} ${new URL(request.url).pathname}`);
    return fixtureFetch(this.#routes, { cursor: this.cursor })(request);
  };

  get acting(): MemberView | undefined {
    return this.session?.member;
  }

  count(call: string): number {
    return this.calls.filter((made) => made === call).length;
  }
}

function renderShell(server: FakeServer, options: { path?: string; retryDelayMs?: number } = {}) {
  const api = createApiClient({ baseUrl: "http://harmony.test", fetch: server.fetch });
  const queryClient = createQueryClient();
  const sources = new EventSources();
  const { retryDelayMs } = options;
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
    openEventSource: sources.open,
    openChannel: () => null,
    reload: vi.fn(),
    ...(retryDelayMs === undefined ? {} : { retryDelayMs: () => retryDelayMs }),
  });
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <RouterProvider router={createMemoryRouter(testRoutes, { initialEntries: [options.path ?? "/"] })} />
    </AppProviders>,
  );
  return { dispatcher, sources, queryClient };
}

/** Render as `session`, past the welcome, with the stream open. */
async function renderLive(session: SessionView, options: { path?: string } = {}) {
  demoStore.setState({ welcomed: true });
  const server = new FakeServer(session);
  const rendered = renderShell(server, options);
  await act(() => rendered.dispatcher.start());
  await screen.findByRole("button", { name: `Account: ${session.member.name}` });
  act(() => {
    rendered.sources.latest.accept();
  });
  return { server, ...rendered };
}

/** Radix menus open on keyboard as well as pointer; jsdom has no pointer events. */
async function openAccountMenu(name: string): Promise<HTMLElement> {
  const trigger = await screen.findByRole("button", { name: `Account: ${name}` });
  fireEvent.keyDown(trigger, { key: "Enter" });
  return screen.findByRole("menu");
}

/** The demo bar. (A banner by role, but hidden from role queries while a modal is up.) */
function demoBar(): HTMLElement {
  const bar = document.querySelector<HTMLElement>('header[aria-label="Demo controls"]');
  if (bar === null) throw new Error("no demo bar");
  return bar;
}

/** Records whether `text` is ever in the document, including between React commits. */
function watchFor(text: string): { seen: () => boolean; stop: () => void } {
  let seen = document.body.textContent.includes(text);
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.target.textContent?.includes(text) === true) seen = true;
      for (const node of record.addedNodes) {
        if (node.textContent?.includes(text) === true) seen = true;
      }
    }
  });
  observer.observe(document.body, { subtree: true, childList: true, characterData: true });
  return {
    seen: () => seen,
    stop: () => {
      observer.disconnect();
    },
  };
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
  window.localStorage.clear();
  demoStore.setState({ welcomed: false, controlsOpen: false, controlsTab: "overview", resetConfirmOpen: false });
  sidebarStore.setState({ collapsed: false });
  toastStore.setState({ toasts: [] });
  clearAllAlerts();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("introduces the demo, starts it as Alex and switches accounts over a fresh stream", async () => {
  const server = new FakeServer(null);
  const { dispatcher, sources } = renderShell(server);
  await act(() => dispatcher.start());

  // First visit, no session: the landing, with the demo controls open over it.
  // (The open modal hides the page from the accessibility tree, so find it by text.)
  expect(screen.getByText("Welcome to Harmony")).toBeTruthy();
  const controls = await screen.findByRole("dialog", { name: "The Harmony demo" });
  expect(sources.opened).toHaveLength(0);

  fireEvent.click(within(controls).getByRole("button", { name: "Try the suggested request" }));

  // Starting creates the session; the shell comes up as Alex with Recents.
  await screen.findByRole("button", { name: "Account: Alex Chen" });
  expect(await screen.findByText(RECENT)).toBeTruthy();
  expect(screen.queryByRole("dialog", { name: "The Harmony demo" })).toBeNull();
  expect(demoStore.getState().welcomed).toBe(true);
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
  expect(within(toastRegion).queryByText(/Henderson Lab needs you/)).toBeNull();
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
  fireEvent.click(screen.getByRole("button", { name: /Henderson Lab needs a human.*Switch to Alex/ }));

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
  const controls = await screen.findByRole("dialog", { name: "The Harmony demo" });
  fireEvent.click(within(controls).getByRole("button", { name: "Try the suggested request" }));

  await screen.findByRole("button", { name: "Account: Alex Chen" });
  await waitFor(() => {
    expect(screen.queryByRole("dialog", { name: "The Harmony demo" })).toBeNull();
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
