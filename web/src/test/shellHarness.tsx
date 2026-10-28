/**
 * The shell under test (shell.test.tsx and the areas that render inside it): the
 * shell's routes with stand-ins for the other areas' pages, a fake API server whose
 * cursor moves as the real one's does (and whose passcode gate can lock), and helpers
 * to render the app live, open the account menu, find the demo bar and watch for
 * transient text.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider, useLocation, type RouteObject } from "react-router";
import { vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import type { DemoWaiting, Inbox, MemberView, SessionView } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import type { BudgetView } from "../app/budget";
import { fetchStreamSnapshot } from "../app/snapshot";
import { demoStore } from "../demo/store";
import { fixtureFetch, json, problem, type FixtureRoutes } from "../fixtures";
import {
  budgets,
  conversations,
  inbox,
  memberDirectory,
  members,
  sessionAs,
  shellRoutes,
  WORLD_ID,
} from "../fixtures/shell";
import { RealtimeDispatcher, type ChannelLike } from "../realtime/dispatcher";
import { EventSources } from "../realtime/testing";
import { AppLayout } from "../shell/AppLayout";
import { OpenEscalation } from "../shell/OpenEscalation";
import { RootLayout } from "../shell/RootLayout";

/** The world `POST /api/worlds/reset` moves the session to. */
export const NEW_WORLD_ID = "0b1d7a3e-5c2f-4e8a-9a51-000000000002";

/** The shell's routes with stand-ins for the areas other tasks own. */
export function Page({ name }: { name: string }) {
  const location = useLocation();
  return (
    <p>
      {name} at {location.pathname}
      {location.search}
    </p>
  );
}

export const testRoutes: RouteObject[] = [
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
export class Gate {
  readonly #opened = Promise.withResolvers<undefined>();
  readonly promise: Promise<void> = this.#opened.promise;

  open(): void {
    this.#opened.resolve(undefined);
  }
}

/** The shell fixtures behind a cursor that moves on act-as and reset, as the API's does. */
export class FakeServer {
  session: SessionView | null;
  cursor = 7;
  /** While set, `GET /api/session` waits for it. */
  sessionGate: Gate | null = null;
  /** While true, `GET /api/session` answers 503. */
  down = false;
  /** While set, act-as answers with it instead of switching. */
  actAsFailure: (() => Response) | null = null;
  /** While true, the passcode gate refuses every request, as with no `harmony_gate` cookie. */
  locked = false;
  /** What `GET /api/budget` answers. */
  budget: BudgetView = budgets.ok;
  /** What `GET /api/demo/waiting` answers: no account has an open item. */
  waiting: DemoWaiting = { accounts: [] };
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
      "GET /api/budget": () => this.budget,
      "GET /api/demo/waiting": () => this.waiting,
      "POST /api/gate/lock": () => {
        this.locked = true;
        return new Response(null, { status: 204 });
      },
    };
  }

  readonly fetch = (request: Request): Promise<Response> => {
    this.calls.push(`${request.method} ${new URL(request.url).pathname}`);
    if (this.locked) return Promise.resolve(problem(401, "passcode_required", "passcode required"));
    return fixtureFetch(this.#routes, { cursor: this.cursor })(request);
  };

  get acting(): MemberView | undefined {
    return this.session?.member;
  }

  count(call: string): number {
    return this.calls.filter((made) => made === call).length;
  }
}

export interface RenderShellOptions {
  path?: string;
  retryDelayMs?: number;
  /** The visitor's tab channel (a `ChannelHub`'s `open`); none by default. */
  openChannel?: (name: string) => ChannelLike | null;
}

export function renderShell(server: FakeServer, options: RenderShellOptions = {}) {
  const api = createApiClient({ baseUrl: "http://harmony.test", fetch: server.fetch });
  const queryClient = createQueryClient();
  const sources = new EventSources();
  const router = createMemoryRouter(testRoutes, { initialEntries: [options.path ?? "/"] });
  const reload = vi.fn<() => void>();
  // Where the tab went when it locked: the passcode page, outside the SPA.
  const leave = vi.fn<(url: string) => void>();
  const { retryDelayMs } = options;
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
    openEventSource: sources.open,
    openChannel: options.openChannel ?? (() => null),
    reload,
    here: () => {
      const { pathname, search, hash } = router.state.location;
      return pathname + search + hash;
    },
    leave,
    ...(retryDelayMs === undefined ? {} : { retryDelayMs: () => retryDelayMs }),
  });
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { dispatcher, sources, queryClient, reload, leave };
}

/** Render as `session`, past the welcome, with the stream open. */
export async function renderLive(session: SessionView, options: Omit<RenderShellOptions, "retryDelayMs"> = {}) {
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
export async function openAccountMenu(name: string): Promise<HTMLElement> {
  const trigger = await screen.findByRole("button", { name: `Account: ${name}` });
  fireEvent.keyDown(trigger, { key: "Enter" });
  return screen.findByRole("menu");
}

/** The demo bar. (A banner by role, but hidden from role queries while a modal is up.) */
export function demoBar(): HTMLElement {
  const bar = document.querySelector<HTMLElement>('header[aria-label="Demo controls"]');
  if (bar === null) throw new Error("no demo bar");
  return bar;
}

/** Records whether `text` is ever in the document, including between React commits. */
export function watchFor(text: string): { seen: () => boolean; stop: () => void } {
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
