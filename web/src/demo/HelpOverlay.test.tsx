// @vitest-environment jsdom
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { members, sessionAs } from "../fixtures/shell";
import { renderLive } from "../test/shellHarness";
import { demoStore } from "./store";

/**
 * Where each control sits in jsdom's 1024 × 768 window, by its `data-help` key. The live
 * budget has no box, as when it isn't shown.
 */
const BOXES: Record<string, [x: number, y: number, width: number, height: number]> = {
  "demo-controls": [62, 7, 121, 24],
  network: [187, 7, 116, 24],
  "acting-as": [320, 10, 267, 18],
  reset: [917, 7, 68, 24],
  requests: [9, 236, 255, 32],
};

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
  demoStore.setState({
    introOpen: false,
    controlsOpen: false,
    resetConfirmOpen: false,
    userGuide: false,
    guideTip: false,
    guideTipShown: false,
    introHelp: false,
  });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const box = BOXES[this.getAttribute("data-help") ?? ""];
    return box === undefined ? new DOMRect() : new DOMRect(...box);
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("says what each key control does from the demo bar's ?, and closes on Escape or Close this guide", async () => {
  const { dispatcher } = await renderLive(sessionAs(members.alex));
  const help = screen.getByRole("button", { name: "What's on screen" });

  fireEvent.click(help);
  const overlay = await screen.findByRole("dialog", { name: "What's on screen" });
  for (const line of [
    "You can turn on dangerous permission skipping to stop agents from asking for human intervention.",
    "Network view lets you see what's going on in the Harmony Network and switch accounts. It's a demo-only feature.",
    "This is the person and organization you're currently acting as.",
    "Reset starts a fresh copy of the network, back at the start of the story.",
    "Requests is an inbox where you receive messages from other Claude agents.",
  ]) {
    expect(within(overlay).getByText(line)).toBeTruthy();
  }
  // Who's on the network, by kind.
  const network = within(overlay).getByRole("region", { name: "This network" });
  expect(within(network).getByText(/^Labs, shops and suppliers/)).toBeTruthy();
  expect(within(network).getByText("7 organizations")).toBeTruthy();
  expect(within(network).getByTitle("Northlake Characterization Core")).toBeTruthy();
  // A control that isn't on screen goes without its callout.
  const shown = within(overlay).getAllByRole("listitem");
  expect(shown).toHaveLength(5);
  expect(shown.map((item) => item.textContent).join(" ")).not.toContain("budget");
  expect(document.activeElement).toBe(within(overlay).getByRole("button", { name: "Close this guide" }));

  fireEvent.keyDown(overlay, { key: "Escape" });
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  await waitFor(() => {
    expect(document.activeElement).toBe(help);
  });

  // Opened from the "?" itself, nothing points the "?" out.
  expect(screen.queryByText("See this guide any time")).toBeNull();

  // A click elsewhere on it, the scrim included, leaves it open; Close this guide closes it.
  fireEvent.click(help);
  const reopened = await screen.findByRole("dialog", { name: "What's on screen" });
  fireEvent.click(reopened);
  expect(screen.getByRole("dialog", { name: "What's on screen" })).toBeTruthy();
  fireEvent.click(within(reopened).getByRole("button", { name: "Close this guide" }));
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  dispatcher.stop();
});

it("opens from Demo controls' See user guide, in place of the modal", async () => {
  const { dispatcher } = await renderLive(sessionAs(members.alex));

  fireEvent.click(screen.getByRole("button", { name: "Demo controls" }));
  const controls = await screen.findByRole("dialog", { name: "Demo controls" });
  fireEvent.click(within(controls).getByRole("button", { name: "See user guide" }));

  const overlay = await screen.findByRole("dialog", { name: "What's on screen" });
  expect(within(overlay).getByText("This is the person and organization you're currently acting as.")).toBeTruthy();
  await waitFor(() => {
    expect(screen.queryByRole("dialog", { name: "Demo controls" })).toBeNull();
  });

  fireEvent.keyDown(overlay, { key: "Escape" });
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  // The first time, a tip points at the "?", and its × dismisses it.
  const tip = await screen.findByText("See this guide any time");
  fireEvent.click(within(tip).getByRole("button", { name: "Dismiss" }));
  await waitFor(() => {
    expect(screen.queryByText("See this guide any time")).toBeNull();
  });

  // Once is enough.
  fireEvent.click(screen.getByRole("button", { name: "Demo controls" }));
  fireEvent.click(
    within(await screen.findByRole("dialog", { name: "Demo controls" })).getByRole("button", { name: "See user guide" }),
  );
  fireEvent.keyDown(await screen.findByRole("dialog", { name: "What's on screen" }), { key: "Escape" });
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  expect(screen.queryByText("See this guide any time")).toBeNull();

  dispatcher.stop();
});
