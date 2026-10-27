// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../api/client";
import { createQueryClient } from "../api/queryClient";
import type { MemberView } from "../api/types";
import { AppProviders } from "../app/AppProviders";
import { fetchStreamSnapshot } from "../app/snapshot";
import { fixtureFetch, type FixtureRequest } from "../fixtures";
import { customizeRoutes, THEIR_SHARING, type CustomizeFixtureOptions } from "../fixtures/customize";
import { members, orgs } from "../fixtures/shell";
import { RealtimeDispatcher } from "../realtime/dispatcher";
import { EventSources } from "../realtime/testing";
import { TooltipProvider } from "../shell/ui";
import { HarmonySettingsScreen } from "./HarmonySettingsPage";

afterEach(() => {
  cleanup();
});

// jsdom doesn't lay out, so it has no scrolling; a linked row scrolls itself into view.
Element.prototype.scrollIntoView = vi.fn();

interface Gate {
  /** Every write, in order: "PUT /api/orgs/{slug}/harmony" and its body. */
  writes: { route: string; body: unknown }[];
  /** Lets the writes through (until then each one waits). */
  open: () => void;
  router: ReturnType<typeof createMemoryRouter>;
}

/** `/customize/harmony?<query>` as Helen (or `actingAs`), with each write held until `gate.open()`. */
function renderSettings(query: string, options: CustomizeFixtureOptions = {}, actingAs: MemberView = members.helen): Gate {
  const base = customizeRoutes(actingAs, options);
  let open: () => void = () => undefined;
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  const writes: Gate["writes"] = [];
  const routes: Record<string, unknown> = { ...base };
  for (const [route, handler] of Object.entries(base)) {
    if (route.startsWith("GET ") || typeof handler !== "function") continue;
    const serve = handler as (request: FixtureRequest) => unknown;
    routes[route] = async (request: FixtureRequest) => {
      writes.push({ route, body: request.body });
      await opened;
      return serve(request);
    };
  }
  const api = createApiClient({ baseUrl: "http://harmony.test", fetch: fixtureFetch(routes) });
  const queryClient = createQueryClient();
  const dispatcher = new RealtimeDispatcher({
    queryClient,
    snapshot: () => fetchStreamSnapshot(api, queryClient),
    openEventSource: new EventSources().open,
    openChannel: () => null,
    reload: vi.fn(),
  });
  const router = createMemoryRouter(
    [
      { path: "/customize/harmony", element: <HarmonySettingsScreen /> },
      { path: "/requests", element: <p>Requests</p> },
    ],
    { initialEntries: [`/customize/harmony?${query}`] },
  );
  render(
    <AppProviders api={api} queryClient={queryClient} dispatcher={dispatcher}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </AppProviders>,
  );
  return { writes, open, router };
}

const SETTINGS = "PUT /api/orgs/{slug}/harmony";
const TRUST = "POST /api/orgs/{slug}/trusted-organizations";

it("keeps a save in flight when the admin types, and applies its result", async () => {
  const gate = renderSettings("section=instructions");
  const box = await screen.findByRole("textbox", { name: "Instructions" });

  fireEvent.change(box, { target: { value: "A" } });
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => {
    expect(gate.writes).toHaveLength(1);
  });

  // Typing mid-save neither re-enables Save nor detaches the pending save.
  fireEvent.change(box, { target: { value: "AB" } });
  expect(screen.getByRole("button", { name: "Saving…" })).toHaveProperty("disabled", true);

  act(() => {
    gate.open();
  });
  // The save landed: only what was typed since is left unsaved.
  const again = await screen.findByRole("button", { name: "Save changes" });
  expect(box).toHaveProperty("value", "AB");
  expect(gate.writes[0]).toEqual({ route: SETTINGS, body: { policy_version: 7, instructions: "A" } });

  fireEvent.click(again);
  await waitFor(() => {
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
  });
  // Based on the version the first save returned: no 409 from its own earlier save.
  expect(gate.writes[1]).toEqual({ route: SETTINGS, body: { policy_version: 7, instructions: "AB" } });
  expect(screen.queryByText(/Another admin/)).toBeNull();
});

it("replays the edits on another admin's newer version after a 409", async () => {
  const gate = renderSettings("section=sharing", { conflict: true });
  gate.open();
  const box = await screen.findByRole("textbox", { name: "Shared with everyone" });
  fireEvent.change(box, { target: { value: "Prices only." } });
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

  // Their description landed first: both admins changed it, so the bar says so.
  await screen.findByText(/Another admin also changed what you share/);
  expect(box).toHaveProperty("value", "Prices only.");
  fireEvent.click(screen.getByRole("button", { name: "Use theirs" }));
  await waitFor(() => {
    expect(box).toHaveProperty("value", THEIR_SHARING);
  });
});

it("trusts the organization a request link names, with what to also share", async () => {
  const gate = renderSettings(`section=sharing&org=${orgs.henderson.id}`);
  gate.open();
  fireEvent.click(await screen.findByRole("button", { name: "Add Henderson Lab" }));
  // Rows are by name: Henderson Lab comes first.
  const [also] = screen.getAllByRole("textbox", { name: "Also share with them" });
  if (also === undefined) throw new Error("no row for Henderson Lab");
  fireEvent.change(also, { target: { value: "Internal reserve slots." } });
  expect(screen.getByRole("button", { name: "Mode for Henderson Lab: Same as default" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

  // Once saved, the row carries who changed it: Helen, today.
  await screen.findByText("Changed by Dr. Helen Ward, Oct 26");
  expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
  expect(gate.writes).toEqual([
    {
      route: TRUST,
      body: { counterparty_org_id: orgs.henderson.id, autonomy: null, sharing_description: "Internal reserve slots." },
    },
  ]);
});

it("shows a trusted row's every mode in full, labels lined up", async () => {
  renderSettings(`section=sharing&org=${orgs.henderson.id}`).open();
  fireEvent.click(await screen.findByRole("button", { name: "Add Henderson Lab" }));
  fireEvent.keyDown(screen.getByRole("button", { name: "Mode for Henderson Lab: Same as default" }), { key: "Enter" });
  const menu = await screen.findByRole("menu");
  // Wide enough for two-line descriptions, and no cap but the viewport's.
  expect(menu.style.width).toBe("400px");
  expect(menu.className).not.toMatch(/max-h-\[\d+px\]/);
  expect(menu.className).toContain("max-h-[var(--radix-dropdown-menu-content-available-height)]");
  const rows = within(menu).getAllByRole("menuitemradio");
  expect(rows.map((row) => row.querySelector(".block")?.textContent)).toEqual(["Same as default", "Ask", "Auto", "Skip"]);
  // "Same as default" has no icon but keeps the slot, so its label starts with the others.
  for (const row of rows) expect(row.firstElementChild?.firstElementChild?.className).toBe("flex w-5 justify-center");
  expect(rows[0]?.querySelector(".w-5")?.childElementCount).toBe(0);
});

it("edits a member's profile on the Team pane and saves it with the settings", async () => {
  const gate = renderSettings("section=team");
  gate.open();
  const box = await screen.findByRole("textbox", { name: "Raj Patel's profile" });
  fireEvent.change(box, { target: { value: "XRD and SEM queues; owns the reserve block." } });
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

  await waitFor(() => {
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
  });
  expect(box).toHaveProperty("value", "XRD and SEM queues; owns the reserve block.");
  expect(gate.writes).toEqual([
    {
      route: SETTINGS,
      body: { policy_version: 7, member_profiles: { [members.raj.id]: "XRD and SEM queues; owns the reserve block." } },
    },
  ]);
});

it("says the sharing check runs only in Auto, not that every answer is checked", async () => {
  renderSettings("section=sharing").open();
  await screen.findByRole("textbox", { name: "Shared with everyone" });
  const sharing = screen.getByText(/What your representative may pass on/).textContent;
  expect(sharing).toContain("In Auto, Claude also checks each answer against it");
  expect(sharing).toContain("in Skip nothing checks it");
  expect(sharing).not.toContain("checks every answer");

  cleanup();
  renderSettings("section=instructions").open();
  await screen.findByRole("textbox", { name: "Instructions" });
  const callout = screen.getByText(/Your representative doesn.t see your files/).textContent;
  expect(callout).toContain("drafts the answer with what you share in mind");
  expect(callout).not.toContain("checks the answer");
});

it("asks before leaving with unsaved edits, and only when leaving the page", async () => {
  const { router, open } = renderSettings("section=instructions");
  open();
  const box = await screen.findByRole("textbox", { name: "Instructions" });
  fireEvent.change(box, { target: { value: "Check with Raj before sharing reserve slots." } });

  const unload = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);

  // Another pane keeps the draft, so it doesn't ask.
  await act(() => router.navigate("/customize/harmony?section=sharing"));
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(() => router.navigate("/customize/harmony?section=instructions"));

  act(() => {
    void router.navigate("/requests");
  });
  const ask = await screen.findByRole("dialog", { name: "Discard unsaved changes?" });
  fireEvent.click(within(ask).getByRole("button", { name: "Keep editing" }));
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  expect(router.state.location.pathname).toBe("/customize/harmony");
  expect(screen.getByRole("textbox", { name: "Instructions" })).toHaveProperty(
    "value",
    "Check with Raj before sharing reserve slots.",
  );

  act(() => {
    void router.navigate("/requests");
  });
  const again = await screen.findByRole("dialog", { name: "Discard unsaved changes?" });
  fireEvent.click(within(again).getByRole("button", { name: "Discard changes" }));
  await screen.findByText("Requests");
});

it("leaves without asking when nothing is unsaved", async () => {
  const { router, open } = renderSettings("section=instructions");
  open();
  await screen.findByRole("textbox", { name: "Instructions" });
  const unload = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(false);
  await act(() => router.navigate("/requests"));
  expect(screen.getByText("Requests")).toBeTruthy();
});

describe("sending Harmony Network Requests", () => {
  const SEND = "Sending Harmony Network Requests";

  it("saves the organization's default, warning while it sends without review", async () => {
    const gate = renderSettings("section=autonomy");
    gate.open();
    const modes = await screen.findByRole("radiogroup", { name: SEND });
    expect(within(modes).getByRole("radio", { name: /Review before sending/ })).toHaveProperty("checked", true);
    expect(screen.queryByText("Requests go out before anyone reads them.")).toBeNull();
    screen.getByText("Members can change this for one chat from the composer's + menu.");

    fireEvent.click(within(modes).getByRole("radio", { name: /Send without review/ }));
    expect(screen.getByRole("alert").textContent).toContain("Requests go out before anyone reads them.");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    });
    expect(gate.writes).toEqual([{ route: SETTINGS, body: { policy_version: 7, send_without_confirmation: true } }]);
    expect(within(modes).getByRole("radio", { name: /Send without review/ })).toHaveProperty("checked", true);
    expect(screen.getByRole("alert").textContent).toContain("Requests go out before anyone reads them.");
  });

  it("is read-only for a member who isn't an admin", async () => {
    renderSettings("section=autonomy", { patch: { send_without_confirmation: true } }, members.raj).open();
    const modes = await screen.findByRole("radiogroup", { name: SEND });

    const send = within(modes).getByRole("radio", { name: /Send without review/ });
    expect(send).toHaveProperty("checked", true);
    for (const radio of within(modes).getAllByRole("radio")) expect(radio).toHaveProperty("disabled", true);
    expect(screen.getByRole("alert").textContent).toContain("Requests go out before anyone reads them.");
  });
});
