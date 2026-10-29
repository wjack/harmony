// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, expect, it } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import { AppProviders } from "../app/AppProviders";
import { fetchStreamSnapshot } from "../app/snapshot";
import { fixtureFetch } from "../fixtures";
import { members, shellRoutes } from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { EventSources } from "../realtime/testing";
import { toastStore } from "../shell/toasts";
import { Page } from "../test/shellHarness";
import { ALEX_CHAT, EARLIER_ID, OTHERS_THREAD, whoseRoutes } from "./fixtures";
import { WhoseNotFound } from "./WhoseNotFound";

beforeEach(() => {
  toastStore.setState({ toasts: [] });
});

afterEach(() => {
  cleanup();
});

/** The page for `kind` at `path`, acting as Helen; returns the API calls it makes. */
function renderAs(path: string, kind: "chat" | "thread", id: string): { calls: string[]; location: () => string } {
  const calls: string[] = [];
  const serve = fixtureFetch({ ...shellRoutes(members.helen), ...whoseRoutes });
  const api = createApiClient({
    baseUrl: "http://harmony.test",
    fetch: async (request) => {
      calls.push(`${request.method} ${new URL(request.url).pathname} ${await request.clone().text()}`);
      return serve(request);
    },
  });
  const queryClient = createQueryClient();
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
    openEventSource: new EventSources().open,
    openChannel: () => null,
  });
  const router = createMemoryRouter(
    [
      { path, element: <WhoseNotFound kind={kind} id={id} /> },
      { path: "*", element: <Page name="Section" /> },
    ],
    { initialEntries: [path] },
  );
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { calls, location: () => router.state.location.pathname };
}

it("says whose chat it is, and switching to them stays on it", async () => {
  const { calls, location } = renderAs(`/chat/${ALEX_CHAT}`, "chat", ALEX_CHAT);

  expect(await screen.findByRole("heading", { name: "This chat is Alex Chen's" })).toBeTruthy();
  expect(
    screen.getByText("Chats belong to the person who started them. You're acting as Dr. Helen Ward at Northlake."),
  ).toBeTruthy();

  const switchTo = screen.getByRole("button", { name: "Switch to Alex" });
  await waitFor(() => {
    expect((switchTo as HTMLButtonElement).disabled).toBe(false);
  });
  fireEvent.click(switchTo);

  await waitFor(() => {
    expect(toastStore.getState().toasts.map((toast) => toast.text)).toEqual(["Switched to Alex Chen"]);
  });
  expect(calls).toContain(`POST /api/session/act-as {"member_id":"${members.alex.id}"}`);
  expect(location()).toBe(`/chat/${ALEX_CHAT}`);
});

it("names a request's two sides and who can open it, with a switch per side", async () => {
  renderAs(`/requests/${OTHERS_THREAD}`, "thread", OTHERS_THREAD);

  expect(await screen.findByRole("heading", { name: "Helen can't open this request" })).toBeTruthy();
  expect(
    screen.getByText(
      "It's between Henderson Frame Lab and Ridgeline Fabrication. Alex Chen and Prof. Maya Henderson at Henderson, and Dana Okafor at Ridgeline Fabrication, can open it.",
    ),
  ).toBeTruthy();
  expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual([
    "Switch to Alex",
    "Switch to Dana",
    "Go to your requests",
  ]);

  fireEvent.click(screen.getByRole("button", { name: "Go to your requests" }));
  expect(await screen.findByText("Section at /requests")).toBeTruthy();
});

it("says an id from an earlier demo isn't in this one, for a chat and a request", async () => {
  renderAs(`/chat/${EARLIER_ID}`, "chat", EARLIER_ID);
  expect(await screen.findByRole("heading", { name: "This chat isn't in this demo" })).toBeTruthy();
  expect(screen.getByText("It's from before a reset or an earlier visit. Nothing from that demo carries over.")).toBeTruthy();
  expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["Go to your chats"]);
  cleanup();

  renderAs(`/requests/${EARLIER_ID}`, "thread", EARLIER_ID);
  expect(await screen.findByRole("heading", { name: "This request isn't in this demo" })).toBeTruthy();
  expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual(["Go to your requests"]);
});
