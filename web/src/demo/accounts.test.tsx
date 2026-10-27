// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import type { SessionView } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import { fetchStreamSnapshot } from "../app/snapshot";
import { json } from "../fixtures";
import { conversations, members, SESSION_ID, sessionAs, WORLD_ID } from "../fixtures/shell";
import { RealtimeDispatcher, TAB_CHANNEL, type MemberChangedMessage } from "../realtime/dispatcher";
import { ChannelHub, EventSources } from "../realtime/testing";
import { toastStore } from "../shell/toasts";
import { FakeServer, openAccountMenu, testRoutes } from "../test/shellHarness";
import { clearAllAlerts } from "./alerts";
import { memberChangedEnvelope } from "./fixtures";
import { demoStore } from "./store";
import { setUnsaved, unsavedSources } from "./unsaved";

const ALEX_CHAT = `/chat/${conversations[0]?.id ?? ""}`;
const SWITCHED = "Switched to Dr. Helen Ward";
const DROPPED = "Your unsaved Customize changes were dropped when you switched to Dr. Helen Ward in another tab.";

let hub: ChannelHub;
const unsubscribes: (() => void)[] = [];

beforeEach(() => {
  hub = new ChannelHub();
  demoStore.setState({ welcomed: true, controlsOpen: false });
  toastStore.setState({ toasts: [] });
  clearAllAlerts();
});

afterEach(() => {
  cleanup();
  for (const unsubscribe of unsubscribes.splice(0)) unsubscribe();
});

/** One tab as Alex on his chat, recording where it goes and every toast it raises. */
async function openTab() {
  const server = new FakeServer(sessionAs(members.alex, 1));
  const api = createApiClient({ baseUrl: "http://harmony.test", fetch: server.fetch });
  const queryClient = createQueryClient();
  const sources = new EventSources();
  const reload = vi.fn<() => void>();
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
    openEventSource: sources.open,
    openChannel: hub.open,
    reload,
  });
  const router = createMemoryRouter(testRoutes, { initialEntries: [ALEX_CHAT] });
  const visits: string[] = [];
  let key = router.state.location.key;
  router.subscribe((state) => {
    if (state.location.key === key) return;
    key = state.location.key;
    visits.push(`${state.historyAction} ${state.location.pathname}`);
  });
  const toasts = new Map<number, string>();
  unsubscribes.push(
    toastStore.subscribe(({ toasts: shown }) => {
      for (const { id, text } of shown) toasts.set(id, text);
    }),
  );
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  await act(() => dispatcher.start());
  await screen.findByRole("button", { name: "Account: Alex Chen" });
  act(() => {
    sources.latest.accept();
  });
  const raised = (text: string) => [...toasts.values()].filter((shown) => shown === text).length;
  return { server, dispatcher, sources, reload, visits, raised };
}

/** The server commits the switch to Helen; `report` tells the tab before it answers. */
function switchesToHelen(server: FakeServer, report: (session: SessionView) => void): void {
  server.actAsFailure = () => {
    const session = sessionAs(members.helen, 2);
    server.cursor += 5;
    server.session = session;
    report(session);
    return json(session, 200, server.cursor);
  };
}

const toHelen: MemberChangedMessage = {
  type: "member_changed",
  sessionId: SESSION_ID,
  epoch: 2,
  memberId: members.helen.id,
};

async function switchToHelen(): Promise<void> {
  const menu = await openAccountMenu(members.alex.name);
  fireEvent.click(within(menu).getByRole("menuitemradio", { name: /Dr\. Helen Ward/ }));
  await screen.findByRole("button", { name: "Account: Dr. Helen Ward" });
  await screen.findByText("Home at /");
}

it("lands a switch once when its event arrives before the response", async () => {
  const { server, dispatcher, sources, visits, raised } = await openTab();
  switchesToHelen(server, (session) => {
    sources.latest.send(memberChangedEnvelope(server.cursor, session));
  });

  await switchToHelen();

  expect(visits).toEqual(["PUSH /"]);
  await waitFor(() => {
    expect(raised(SWITCHED)).toBe(1);
  });
  expect(dispatcher.state).toMatchObject({ epoch: 2, generation: 2 });
  dispatcher.stop();
});

it("lands a switch once when another tab hears of it first", async () => {
  const { server, dispatcher, visits, raised, reload } = await openTab();
  const otherTab = hub.open(TAB_CHANNEL);
  switchesToHelen(server, () => {
    otherTab.postMessage(toHelen);
  });

  await switchToHelen();

  expect(visits).toEqual(["PUSH /"]);
  await waitFor(() => {
    expect(raised(SWITCHED)).toBe(1);
  });
  expect(reload).not.toHaveBeenCalled();
  dispatcher.stop();
});

it("follows another tab's switch in place, off the previous member's chat, and says unsaved edits went", async () => {
  const { server, dispatcher, visits, raised, reload } = await openTab();
  setUnsaved("customize", true);

  // Another tab switched: the server acts as Helen, and that tab says so.
  server.session = sessionAs(members.helen, 2, WORLD_ID);
  act(() => {
    hub.open(TAB_CHANNEL).postMessage(toHelen);
  });

  await screen.findByRole("button", { name: "Account: Dr. Helen Ward" });
  expect(await screen.findByText("Home at /")).toBeTruthy();
  expect(visits).toEqual(["REPLACE /"]);
  await waitFor(() => {
    expect(raised(DROPPED)).toBe(1);
  });
  expect(raised(SWITCHED)).toBe(0);
  expect(unsavedSources()).toEqual([]);
  expect(reload).not.toHaveBeenCalled();
  dispatcher.stop();
});
