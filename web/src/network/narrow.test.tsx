// @vitest-environment jsdom
import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { sidebarStore } from "../shell/sidebarStore";
import { useRailWhilePanelOpen } from "./narrow";

function windowWidth(width: number): void {
  vi.stubGlobal("matchMedia", (media: string) => ({
    media,
    matches: width < 1200,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sidebarStore.setState({ collapsed: false, rail: false });
});

it("shows the sidebar's rail while the org panel is open on a narrow window", () => {
  windowWidth(1024);
  const hook = renderHook((open: boolean) => {
    useRailWhilePanelOpen(open);
  }, { initialProps: false });
  expect(sidebarStore.getState().rail).toBe(false);

  hook.rerender(true);
  expect(sidebarStore.getState().rail).toBe(true);
  // The member's own choice is left alone.
  expect(sidebarStore.getState().collapsed).toBe(false);

  hook.rerender(false);
  expect(sidebarStore.getState().rail).toBe(false);
});

it("leaves the sidebar alone on a wide window", () => {
  windowWidth(1440);
  renderHook(() => {
    useRailWhilePanelOpen(true);
  });
  expect(sidebarStore.getState().rail).toBe(false);
});
