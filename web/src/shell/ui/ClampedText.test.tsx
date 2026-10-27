// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ClampedText } from "./ClampedText";

/** jsdom lays nothing out: each test says how tall the text is, clamped and in full. */
let heights = { scroll: 0, client: 0 };
let resized: (() => void)[] = [];

function resize(next: { scroll: number; client: number }): void {
  heights = next;
  act(() => {
    for (const callback of resized) callback();
  });
}

beforeEach(() => {
  resized = [];
  vi.stubGlobal(
    "ResizeObserver",
    class {
      readonly #callback: () => void;
      constructor(callback: () => void) {
        this.#callback = callback;
      }
      observe(): void {
        resized.push(this.#callback);
      }
      unobserve(): void {}
      disconnect(): void {
        resized = resized.filter((callback) => callback !== this.#callback);
      }
    },
  );
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(() => heights.scroll);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => heights.client);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const toggle = () => screen.queryByRole("button", { name: /^View (more|less)$/ });

it("shows no toggle while the text fits in its lines", () => {
  render(<ClampedText lines={3}>Two short lines.</ClampedText>);
  resize({ scroll: 40, client: 40 });
  expect(toggle()).toBeNull();
});

it("shows a toggle for the rest when the clamp cuts text off, and folds it back", () => {
  render(<ClampedText lines={3}>A brief long enough to run past three lines.</ClampedText>);
  resize({ scroll: 120, client: 60 });
  const more = screen.getByRole("button", { name: "View more" });
  expect(more.getAttribute("aria-expanded")).toBe("false");

  fireEvent.click(more);
  const less = screen.getByRole("button", { name: "View less" });
  expect(less.getAttribute("aria-expanded")).toBe("true");
  // Expanded, nothing is cut off any more, and the toggle stays to fold it back.
  resize({ scroll: 120, client: 120 });
  expect(toggle()?.textContent).toBe("View less");

  fireEvent.click(less);
  resize({ scroll: 120, client: 60 });
  expect(toggle()?.textContent).toBe("View more");
});

it("follows the box: the toggle comes and goes as a resize cuts text off or stops", () => {
  render(<ClampedText lines={2}>Text that only overflows in a narrow card.</ClampedText>);
  resize({ scroll: 40, client: 40 });
  expect(toggle()).toBeNull();
  resize({ scroll: 80, client: 40 });
  expect(toggle()?.textContent).toBe("View more");
  resize({ scroll: 40, client: 40 });
  expect(toggle()).toBeNull();
});

it("measures again when the text changes in a box that keeps its height", async () => {
  const { rerender } = render(<ClampedText lines={2}>A short draft.</ClampedText>);
  resize({ scroll: 40, client: 40 });
  expect(toggle()).toBeNull();

  // The clamp holds the box at two lines, so nothing resizes; only the text grew.
  heights = { scroll: 120, client: 40 };
  await act(async () => {
    rerender(<ClampedText lines={2}>A redraft that runs on well past the two lines the card shows.</ClampedText>);
    await Promise.resolve();
  });
  expect(toggle()?.textContent).toBe("View more");
});
