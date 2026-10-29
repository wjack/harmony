// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CardFoot, DeclineStep, EditableText, PartLabel, useApproveShortcut } from ".";

afterEach(cleanup);

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    },
  );
});

describe("EditableText", () => {
  function Field({ original = "Claude's draft." }: { original?: string }) {
    const [value, setValue] = useState(original);
    return <EditableText value={value} original={original} onChange={setValue} label="Draft reply" />;
  }

  it("is edited in place and keeps its outline once it differs", () => {
    render(<Field />);
    const field = screen.getByRole("textbox", { name: "Draft reply" });
    expect(field).toHaveProperty("value", "Claude's draft.");
    expect(field.dataset.edited).toBeUndefined();

    fireEvent.change(field, { target: { value: "My draft." } });
    expect(field.dataset.edited).toBe("true");

    fireEvent.change(field, { target: { value: "Claude's draft." } });
    expect(field.dataset.edited).toBeUndefined();
  });

  it("shows its last line when a web font lands after the first fit and rewraps the text", () => {
    const fonts = new EventTarget();
    Object.defineProperty(document, "fonts", { value: fonts, configurable: true });
    let lines = 2;
    vi.spyOn(HTMLTextAreaElement.prototype, "scrollHeight", "get").mockImplementation(() => lines * 24);
    try {
      render(<Field />);
      const field = screen.getByRole("textbox", { name: "Draft reply" });
      expect(field.style.height).toBe("48px");

      lines = 3;
      fonts.dispatchEvent(new Event("loadingdone"));
      expect(field.style.height).toBe("72px");
    } finally {
      Reflect.deleteProperty(document, "fonts");
    }
  });

  it("can't be changed while disabled", () => {
    render(<EditableText value="Text" original="Text" onChange={vi.fn()} label="Brief" disabled />);
    expect(screen.getByRole("textbox", { name: "Brief" })).toHaveProperty("readOnly", true);
  });
});

describe("PartLabel", () => {
  it("says Claude rewrote it, with a pager through the versions", () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    render(<PartLabel label="Draft reply" rewritten={{ index: 1, count: 2, onPrev, onNext }} />);
    expect(screen.getByText("· rewritten by Claude")).toBeTruthy();
    expect(screen.getByText("2 of 2")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Later version" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "Earlier version" }));
    expect(onPrev).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Undo my edits" })).toBeNull();
  });

  it("doesn't call Claude's original rewritten when the pager is back on it", () => {
    render(<PartLabel label="Draft reply" rewritten={{ index: 0, count: 2, onPrev: vi.fn(), onNext: vi.fn() }} />);
    expect(screen.getByText("1 of 2")).toBeTruthy();
    expect(screen.queryByText("· rewritten by Claude")).toBeNull();
  });

  it("says the member edited it, with Undo my edits", () => {
    const onUndo = vi.fn();
    render(<PartLabel label="Brief" edited={{ onUndo }} />);
    expect(screen.getByText("· edited by you")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Undo my edits" }));
    expect(onUndo).toHaveBeenCalledOnce();
  });

  it("puts Undo my edits before the pager when both apply", () => {
    render(
      <PartLabel
        label="Draft reply"
        rewritten={{ index: 1, count: 2, onPrev: vi.fn(), onNext: vi.fn() }}
        edited={{ onUndo: vi.fn() }}
      />,
    );
    const buttons = screen.getAllByRole("button").map((button) => button.textContent || button.ariaLabel);
    expect(buttons).toEqual(["Undo my edits", "Earlier version", "Later version"]);
    expect(screen.getByText("· rewritten by Claude")).toBeTruthy();
    expect(screen.getByText("· edited by you")).toBeTruthy();
  });
});

describe("CardFoot", () => {
  it("has Decline on the left, Approve and send on the right, and a hint row", () => {
    const onDecline = vi.fn();
    const onApprove = vi.fn();
    const { container } = render(
      <CardFoot
        onDecline={onDecline}
        onApprove={onApprove}
        hintLeft="Ask mode with Henderson Frame Lab"
        hintRight="Claude replies to Henderson Frame Lab with this answer"
      />,
    );
    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Decline",
      "Approve and send",
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Approve and send" }));
    expect(onApprove).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Decline" }));
    expect(onDecline).toHaveBeenCalledOnce();
    expect(screen.getByText("Claude replies to Henderson Frame Lab with this answer")).toBeTruthy();
    // Sticks to the bottom of the scroll area while the card is taller than the view.
    const foot = container.querySelector("[data-card-foot]");
    expect(foot?.className).toMatch(/\bsticky\b/);
    expect(foot?.className).toMatch(/\bbottom-0\b/);
  });

  it("takes another label and turns its buttons off", () => {
    render(
      <CardFoot onDecline={vi.fn()} onApprove={vi.fn()} approveLabel="Send" approveDisabled declineDisabled />,
    );
    expect(screen.getByRole("button", { name: "Send" })).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Decline" })).toHaveProperty("disabled", true);
  });
});

describe("DeclineStep", () => {
  it("needs a reason when required", () => {
    const onConfirm = vi.fn();
    render(<DeclineStep required caption="Claude may pass this reason on." onCancel={vi.fn()} onConfirm={onConfirm} />);
    const decline = screen.getByRole("button", { name: "Decline" });
    expect(decline).toHaveProperty("disabled", true);
    expect(screen.queryByText(/You can leave it empty/)).toBeNull();

    fireEvent.change(screen.getByRole("textbox", { name: "Reason" }), { target: { value: "  Not yet.  " } });
    fireEvent.click(decline);
    expect(onConfirm).toHaveBeenCalledWith("Not yet.");
  });

  it("declines with no reason when it's optional", () => {
    const onConfirm = vi.fn();
    render(
      <DeclineStep
        required={false}
        caption="Only Claude in this chat sees this."
        onCancel={vi.fn()}
        onConfirm={onConfirm}
      />,
    );
    expect(screen.getByText("Only Claude in this chat sees this. You can leave it empty.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Decline" }));
    expect(onConfirm).toHaveBeenCalledWith(null);
  });

  it("cancels on Esc and locks while pending", () => {
    const onCancel = vi.fn();
    const { rerender } = render(
      <DeclineStep required={false} caption="" onCancel={onCancel} onConfirm={vi.fn()} />,
    );
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Reason" }), { key: "Escape" });
    expect(onCancel).toHaveBeenCalledOnce();

    rerender(<DeclineStep required={false} caption="" pending onCancel={onCancel} onConfirm={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Decline" })).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveProperty("disabled", true);
  });
});

describe("useApproveShortcut", () => {
  function Card({ onApprove, enabled = true }: { onApprove: () => void; enabled?: boolean }) {
    const card = useRef<HTMLElement>(null);
    useApproveShortcut(card, onApprove, enabled);
    return (
      <>
        <section ref={card}>
          <textarea aria-label="Draft" />
          <DeclineStep required={false} caption="" onCancel={vi.fn()} onConfirm={vi.fn()} />
        </section>
        <textarea aria-label="Comment" />
      </>
    );
  }

  it("approves on ⌘↵ or Ctrl↵ only with focus inside the card", () => {
    const onApprove = vi.fn();
    render(<Card onApprove={onApprove} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Comment" }), { key: "Enter", metaKey: true });
    expect(onApprove).not.toHaveBeenCalled();

    fireEvent.keyDown(screen.getByRole("textbox", { name: "Draft" }), { key: "Enter", metaKey: true });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Draft" }), { key: "Enter", ctrlKey: true });
    expect(onApprove).toHaveBeenCalledTimes(2);

    fireEvent.keyDown(screen.getByRole("textbox", { name: "Draft" }), { key: "Enter" });
    expect(onApprove).toHaveBeenCalledTimes(2);
  });

  it("leaves ⌘↵ in the Decline step's reason to the step", () => {
    const onApprove = vi.fn();
    render(<Card onApprove={onApprove} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Reason" }), { key: "Enter", metaKey: true });
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("does nothing while off", () => {
    const onApprove = vi.fn();
    render(<Card onApprove={onApprove} enabled={false} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Draft" }), { key: "Enter", metaKey: true });
    expect(onApprove).not.toHaveBeenCalled();
  });
});
