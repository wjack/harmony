// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import { AppProviders } from "../app/AppProviders";
import { useDemoStory } from "../app/queries";
import { fetchStreamSnapshot } from "../app/snapshot";
import { NewChatPage } from "../chat";
import { fixtureFetch } from "../fixtures";
import { shellRoutes, story } from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { EventSources } from "../realtime/testing";
import { AppLayout } from "../shell/AppLayout";
import { RootLayout } from "../shell/RootLayout";
import { demoStore } from "./store";

/** What the network view rings: the story's suggested recipients, as `useDemoStory` gives them. */
function Rings() {
  const recipients = useDemoStory().data?.suggested_recipients ?? [];
  return <p>Rings: {recipients.length === 0 ? "none" : recipients.map((org) => org.slug).join(", ")}</p>;
}

function renderNewChat() {
  const api = createApiClient({ baseUrl: "http://harmony.test", fetch: fixtureFetch(shellRoutes()) });
  const queryClient = createQueryClient();
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
    openEventSource: new EventSources().open,
    openChannel: () => null,
    reload: vi.fn(),
  });
  const router = createMemoryRouter([
    {
      element: <RootLayout />,
      children: [
        {
          element: <AppLayout />,
          children: [
            {
              index: true,
              element: (
                <>
                  <NewChatPage />
                  <Rings />
                </>
              ),
            },
          ],
        },
      ],
    },
  ]);
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return dispatcher;
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
  demoStore.setState({
    welcomed: true,
    controlsOpen: false,
    showSuggestedMessage: true,
    highlightSuggestedRecipients: true,
  });
});

afterEach(() => {
  cleanup();
});

it("hides the suggestion chip and the recipient rings when their walkthrough switches are off", async () => {
  const message = story.suggested_message ?? "";
  const dispatcher = renderNewChat();
  await act(() => dispatcher.start());

  // Alex's new chat suggests the story's opening message; the story names its recipients.
  expect(await screen.findByTitle(message)).toBeTruthy();
  expect(await screen.findByText("Rings: northlake, ridgeline, calder")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Demo controls" }));
  const controls = await screen.findByRole("dialog", { name: "Demo controls" });

  // (The open modal hides the page from role queries, so look for the chip by its title.)
  fireEvent.click(within(controls).getByRole("switch", { name: /Show the suggested message/ }));
  await waitFor(() => {
    expect(screen.queryByTitle(message)).toBeNull();
  });
  expect(screen.getByText("Rings: northlake, ridgeline, calder")).toBeTruthy();

  fireEvent.click(within(controls).getByRole("switch", { name: /Highlight suggested recipients/ }));
  expect(await screen.findByText("Rings: none")).toBeTruthy();

  // Back on, both return; the switches are kept for the visitor's next visit.
  fireEvent.click(within(controls).getByRole("switch", { name: /Show the suggested message/ }));
  expect(await screen.findByTitle(message)).toBeTruthy();
  expect(JSON.parse(window.localStorage.getItem("harmony.demo") ?? "{}")).toMatchObject({
    state: { showSuggestedMessage: true, highlightSuggestedRecipients: false },
  });

  dispatcher.stop();
});
