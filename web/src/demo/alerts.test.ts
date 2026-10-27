// @vitest-environment jsdom
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { members, sessionAs } from "../fixtures/shell";
import { needsHuman } from "../realtime/testing";
import { toastStore } from "../shell/toasts";
import { demoBar, renderLive } from "../test/shellHarness";
import { alertStore, clearAllAlerts } from "./alerts";

// Helen's first inbox item and a second one (fixtures/shell.ts).
const ITEM = "0b1d7a3e-0000-4000-8000-0000000003e1";
const OTHER_ITEM = "0b1d7a3e-0000-4000-8000-0000000003e2";

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
  toastStore.setState({ toasts: [] });
  clearAllAlerts();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function toastTexts(): string[] {
  return toastStore.getState().toasts.map((toast) => `${toast.text} ${toast.action?.label ?? ""}`.trim());
}

it("gives one item routed to two people one toast and one chip, naming the first", async () => {
  const { sources } = await renderLive(sessionAs(members.alex));

  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ITEM));
    sources.latest.send(needsHuman(9, members.raj.slug, ITEM));
  });

  await within(demoBar()).findByText("Switch to Helen");
  await waitFor(() => {
    expect(toastTexts()).toEqual(["Northlake needs a human. Switch to Helen"]);
  });
  expect(within(demoBar()).getAllByText(/needs a human/)).toHaveLength(1);
  expect(within(demoBar()).queryByText("Switch to Raj")).toBeNull();
  expect(alertStore.getState().alerts.map((alert) => alert.memberSlug)).toEqual([members.helen.slug]);

  // Another item is another cue.
  act(() => {
    sources.latest.send(needsHuman(10, members.raj.slug, OTHER_ITEM));
  });
  await within(demoBar()).findByText("Switch to Raj");
});

it("moves the cue to whoever an item is reassigned to", async () => {
  const { sources } = await renderLive(sessionAs(members.alex));

  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ITEM));
  });
  await within(demoBar()).findByText("Switch to Helen");
  // Helen's teammate moves it to Raj: a later commit, so a new routing.
  act(() => {
    sources.latest.send(needsHuman(9, members.raj.slug, ITEM, "northlake", "2026-10-26T09:04:00Z"));
  });

  await within(demoBar()).findByText("Switch to Raj");
  expect(within(demoBar()).queryByText("Switch to Helen")).toBeNull();
  expect(alertStore.getState().alerts.map((alert) => alert.memberSlug)).toEqual([members.raj.slug]);
  await waitFor(() => {
    expect(toastTexts()).toContain("Northlake needs a human. Switch to Raj");
  });
});

it("says it once to the acting member when the item is also routed to a teammate", async () => {
  const { sources } = await renderLive(sessionAs(members.helen));

  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ITEM));
    sources.latest.send(needsHuman(9, members.raj.slug, ITEM));
  });

  await waitFor(() => {
    expect(toastTexts()).toHaveLength(1);
  });
  expect(toastTexts()[0]).toMatch(/needs your answer|needs you/);
  expect(screen.queryByText("Switch to Raj")).toBeNull();
  expect(alertStore.getState().alerts).toEqual([]);
});

it("points a co-assignee at their own item, even when the teammate's cue comes first", async () => {
  const { sources } = await renderLive(sessionAs(members.raj));

  act(() => {
    sources.latest.send(needsHuman(8, members.helen.slug, ITEM));
    sources.latest.send(needsHuman(9, members.raj.slug, ITEM));
  });

  await waitFor(() => {
    expect(toastTexts()).toHaveLength(1);
  });
  expect(toastTexts()[0]).toMatch(/needs your answer|needs you/);
  expect(within(demoBar()).queryByText("Switch to Helen")).toBeNull();
  expect(screen.queryByText(/Switch to Helen/)).toBeNull();
  expect(alertStore.getState().alerts).toEqual([]);
});
