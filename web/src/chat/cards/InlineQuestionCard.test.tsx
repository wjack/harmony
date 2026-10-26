// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import type { InlineQuestionItem } from "../types";
import { InlineQuestionCard } from "./InlineQuestionCard";

afterEach(cleanup);

const OPEN: InlineQuestionItem = {
  id: "esc:1",
  at: "2026-11-02T10:00:00Z",
  type: "inline_question",
  escalation_id: "00000000-0000-4000-8000-000000000001",
  thread_id: "00000000-0000-4000-8000-000000000002",
  question: "What is the project's budget?",
  from_org: { id: "00000000-0000-4000-8000-000000000003", slug: "ridgeline", name: "Ridgeline Fabrication" },
  status: "open",
  action: null,
  answer: null,
};

function show(item: Partial<InlineQuestionItem>): void {
  cleanup();
  render(<InlineQuestionCard item={{ ...OPEN, ...item }} busy={false} error={null} onAnswer={() => undefined} />);
}

it("says how a settled question was settled, not 'Answered' for every one", () => {
  show({ status: "resolved", action: "approve", answer: "About $2,000." });
  expect(screen.getByText("Answered")).toBeTruthy();
  expect(screen.getByText("About $2,000.")).toBeTruthy();

  show({ status: "resolved", action: "decline", answer: "Say we don't share budgets." });
  expect(screen.queryByText("Answered")).toBeNull();
  expect(screen.getByText("Declined")).toBeTruthy();
  expect(screen.getByText("Your note for the representative")).toBeTruthy();

  show({ status: "resolved", action: "take_over", answer: "Alex here: about $2,000." });
  expect(screen.getByText("You replied directly")).toBeTruthy();
  expect(screen.getByText("What you sent")).toBeTruthy();
});

it("keeps a question whose conversation ended, as no longer needed", () => {
  show({ status: "cancelled" });
  expect(screen.getByText("No longer needed")).toBeTruthy();
  expect(screen.getByText("What is the project's budget?")).toBeTruthy();
  expect(screen.queryByRole("textbox")).toBeNull();
});

it("says the scripted demo carries on as recorded whatever the answer, only while it's open", () => {
  show({});
  expect(screen.getByText(/^Scripted demo: Claude replays the recorded story/)).toBeTruthy();
  show({ status: "resolved", action: "approve", answer: "About $2,000." });
  expect(screen.queryByText(/^Scripted demo/)).toBeNull();
});
