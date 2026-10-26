import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { dismissToast, dismissToastWithKey, dismissToastsWithKeyPrefix, toast, toastStore } from "./toasts";

function texts(): string[] {
  return toastStore.getState().toasts.map((shown) => shown.text);
}

beforeEach(() => {
  vi.useFakeTimers();
  toastStore.setState({ toasts: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

it("dismisses a toast after five seconds, unless it's sticky", () => {
  toast("Demo reset");
  toast({ text: "Stays", durationMs: null });
  vi.advanceTimersByTime(4_999);
  expect(texts()).toEqual(["Demo reset", "Stays"]);
  vi.advanceTimersByTime(1);
  expect(texts()).toEqual(["Stays"]);
});

it("replaces a live toast with the same key", () => {
  toast({ text: "Switched to Alex Chen", key: "account-switch" });
  vi.advanceTimersByTime(4_000);
  toast({ text: "Switched to Dr. Helen Ward", key: "account-switch" });
  expect(texts()).toEqual(["Switched to Dr. Helen Ward"]);
  // The replacement gets its own full duration.
  vi.advanceTimersByTime(4_000);
  expect(texts()).toEqual(["Switched to Dr. Helen Ward"]);
});

it("keeps the three newest", () => {
  for (const text of ["one", "two", "three", "four"]) toast(text);
  expect(texts()).toEqual(["two", "three", "four"]);
});

it("dismisses by id or by key", () => {
  const id = toast("By id");
  toast({ text: "By key", key: "needs-human:e1" });
  dismissToast(id);
  dismissToastWithKey("needs-human:e1");
  dismissToastWithKey("not-shown");
  expect(texts()).toEqual([]);
});

it("dismisses every toast of one kind by key prefix", () => {
  toast({ text: "Northlake needs a human.", key: "needs-human:e1" });
  toast({ text: "Ridgeline needs a human.", key: "needs-human:e2" });
  toast({ text: "Demo reset", key: "demo-reset" });
  dismissToastsWithKeyPrefix("needs-human:");
  expect(texts()).toEqual(["Demo reset"]);
});
