// @vitest-environment jsdom
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { alertStore, clearAllAlerts } from "../demo/alerts";
import { demoStore } from "../demo/store";
import { problem } from "../fixtures";
import { members, sessionAs } from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { ChannelHub, EventSources, needsHuman } from "../realtime/testing";
import { toastStore } from "../shell/toasts";
import { FakeServer, openAccountMenu, renderLive, renderShell, watchFor } from "../test/shellHarness";

const ESCALATION = "0b1d7a3e-0000-4000-8000-0000000003e1";

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
  demoStore.setState({ welcomed: true, controlsOpen: false, resetConfirmOpen: false });
  toastStore.setState({ toasts: [] });
  clearAllAlerts();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("locks on the gate's 401 to a mutation: off to the passcode page, with nothing left on screen", async () => {
  const { server, dispatcher, sources, queryClient, leave } = await renderLive(sessionAs(members.alex), {
    path: "/network?focus=edge",
  });
  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ESCALATION));
  });
  await waitFor(() => {
    expect(toastStore.getState().toasts).toHaveLength(1);
  });
  const unreachable = watchFor("Can't reach Harmony");

  // Another tab locked Harmony; this one finds out from its next request.
  server.locked = true;
  const menu = await openAccountMenu(members.alex.name);
  fireEvent.click(within(menu).getByRole("menuitemradio", { name: /Dr\. Helen Ward/ }));

  await screen.findByRole("heading", { name: "Harmony is locked" });
  expect(leave).toHaveBeenCalledExactlyOnceWith("/api/gate?next=%2Fnetwork%3Ffocus%3Dedge");
  expect(dispatcher.state).toMatchObject({ status: "locked", failures: 0 });
  expect(sources.latest.closed).toBe(true);
  expect(sources.opened).toHaveLength(1);
  expect(queryClient.getQueryCache().getAll()).toEqual([]);
  expect(alertStore.getState().alerts).toEqual([]);
  // The cast is gone: no demo bar, no account, no toast (not even the failed switch's).
  expect(screen.queryByRole("status")).toBeNull();
  expect(document.body.textContent).not.toContain(members.alex.name);
  expect(document.body.textContent).not.toContain("Helen");
  expect(unreachable.seen()).toBe(false);
  unreachable.stop();
  expect(server.count("POST /api/session/act-as")).toBe(1);
});

it("still shows the landing, and doesn't lock, on the API's own 401", async () => {
  const server = new FakeServer(null);
  const { dispatcher, leave } = renderShell(server);
  await act(() => dispatcher.start());

  expect(screen.getByText("Welcome to Harmony")).toBeTruthy();
  expect(dispatcher.state.status).toBe("no-session");
  expect(leave).not.toHaveBeenCalled();
});

it("treats a mutation's other 401 as that mutation's failure", async () => {
  const { server, dispatcher, leave } = await renderLive(sessionAs(members.alex));
  server.actAsFailure = () => problem(401, "no_session", "No session.");

  const menu = await openAccountMenu(members.alex.name);
  fireEvent.click(within(menu).getByRole("menuitemradio", { name: /Dr\. Helen Ward/ }));

  expect((await screen.findByRole("status")).textContent).toContain("Couldn't switch to Dr. Helen Ward");
  expect(dispatcher.state.status).not.toBe("locked");
  expect(leave).not.toHaveBeenCalled();
});

it("meets the gate on the first load without showing the landing", async () => {
  const server = new FakeServer(sessionAs(members.alex));
  server.locked = true;
  const landing = watchFor("Welcome to Harmony");
  const { dispatcher, sources, leave } = renderShell(server, { path: "/requests" });
  await act(() => dispatcher.start());

  await screen.findByRole("heading", { name: "Harmony is locked" });
  expect(leave).toHaveBeenCalledExactlyOnceWith("/api/gate?next=%2Frequests");
  expect(sources.opened).toHaveLength(0);
  expect(landing.seen()).toBe(false);
  landing.stop();
});

it("Lock Harmony locks the visitor's other tabs too", async () => {
  const hub = new ChannelHub();
  const { server, dispatcher, leave } = await renderLive(sessionAs(members.alex), { openChannel: hub.open });
  // The visitor's other tab: its own dispatcher on the same channel.
  const reload = vi.fn<() => void>();
  const other = new RealtimeDispatcher({
    queryClient: new QueryClient(),
    snapshot: () => Promise.resolve({ cursor: 7, epoch: 1, sessionId: dispatcher.state.sessionId ?? "" }),
    openEventSource: new EventSources().open,
    openChannel: hub.open,
    reload,
    leave: vi.fn(),
  });
  await other.start();

  const menu = await openAccountMenu(members.alex.name);
  fireEvent.click(within(menu).getByRole("menuitem", { name: "Lock Harmony" }));

  await screen.findByRole("heading", { name: "Harmony is locked" });
  expect(server.count("POST /api/gate/lock")).toBe(1);
  expect(server.locked).toBe(true);
  expect(leave).toHaveBeenCalledExactlyOnceWith("/api/gate?next=%2F");
  expect(reload).toHaveBeenCalledOnce();
  other.stop();
});
