// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { followHides, useStickToBottom } from "./useStickToBottom";

describe("followHides", () => {
  const at = (scrollTop: number, scrollHeight: number) => ({ scrollTop, scrollHeight, clientHeight: 800 });

  it("is true when following the bottom would scroll the top out of view", () => {
    // 600px grew under a view that was at the bottom: following moves everything up 600.
    expect(followHides(500, at(1200, 2600))).toBe(true);
    expect(followHides(500, at(1200, 1900))).toBe(false);
    expect(followHides(500, at(1200, 2000))).toBe(false);
  });
});

/** The resize observers made since the test began, to fire by hand: jsdom lays nothing out. */
let observers: (() => void)[] = [];

beforeEach(() => {
  observers = [];
  // jsdom has no scrolling; `layout` puts a spy on the viewport.
  Element.prototype.scrollTo = () => undefined;
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        observers.push(callback);
      }
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(Element.prototype, "scrollTo");
});

/** A transcript with one disclosure (optionally controlling a region of its own) and a pinned view. */
function Transcript({ controls }: { controls: boolean }) {
  const { scrollRef, contentRef, atBottom, onScroll } = useStickToBottom([]);
  const [open, setOpen] = useState(false);
  return (
    <div ref={scrollRef} data-testid="viewport" onScroll={onScroll}>
      <div ref={contentRef}>
        {controls && <p id="clamped">A long message</p>}
        <button
          type="button"
          aria-expanded={open}
          aria-controls={controls ? "clamped" : undefined}
          onClick={() => {
            setOpen((value) => !value);
          }}
        >
          View more
        </button>
      </div>
      {!atBottom && <p>Scrolled up</p>}
    </div>
  );
}

/**
 * Lays the view out by hand: an 800px viewport at the bottom of `height`px of content,
 * with each element's top (by id, or "toggle") the given px below the viewport's.
 */
function layout(height: number, tops: Readonly<Record<string, number>>) {
  const viewport = screen.getByTestId("viewport");
  let scrollHeight = height;
  Object.defineProperty(viewport, "clientHeight", { configurable: true, get: () => 800 });
  Object.defineProperty(viewport, "scrollHeight", { configurable: true, get: () => scrollHeight });
  viewport.scrollTop = height - 800;
  const scrollTo = vi.fn();
  viewport.scrollTo = scrollTo as typeof viewport.scrollTo;
  for (const [id, top] of Object.entries(tops)) {
    const element = id === "toggle" ? screen.getByRole("button") : document.getElementById(id);
    if (element !== null) element.getBoundingClientRect = () => ({ top }) as DOMRect;
  }
  return {
    scrollTo,
    grow: (by: number) => {
      scrollHeight += by;
      act(() => {
        for (const observer of observers) observer();
      });
    },
  };
}

it("stays where the member reads when what they unfolded would scroll away", () => {
  render(<Transcript controls={false} />);
  const view = layout(2000, { toggle: 500 });
  fireEvent.click(screen.getByRole("button", { name: "View more" }));
  view.grow(600);
  expect(view.scrollTo).not.toHaveBeenCalled();
  expect(screen.getByText("Scrolled up")).toBeTruthy();
});

it("keeps the clamped text's own top, not its toggle's, in view", () => {
  render(<Transcript controls />);
  const view = layout(2000, { toggle: 500, clamped: 200 });
  fireEvent.click(screen.getByRole("button", { name: "View more" }));
  // The toggle would stay in view, but the text it unfolds starts above it.
  view.grow(300);
  expect(view.scrollTo).not.toHaveBeenCalled();
});

it("still follows the bottom when an unfold leaves its top in view, or when content grows on its own", () => {
  render(<Transcript controls={false} />);
  const view = layout(2000, { toggle: 500 });
  fireEvent.click(screen.getByRole("button", { name: "View more" }));
  view.grow(300);
  expect(view.scrollTo).toHaveBeenCalledTimes(1);
  // A card's data arriving: nothing was unfolded.
  view.grow(600);
  expect(view.scrollTo).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("Scrolled up")).toBeNull();
});
