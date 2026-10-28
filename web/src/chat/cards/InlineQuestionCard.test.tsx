// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import type { InlineQuestionItem, OrgRef, QuestionAsker, ResolveEscalation } from "../types";
import { InlineQuestionCard } from "./InlineQuestionCard";

afterEach(cleanup);

function org(n: number, slug: string, name: string): OrgRef {
  return { id: `00000000-0000-4000-8000-00000000000${String(n)}`, slug, name };
}

const NORTHLAKE = org(1, "northlake", "Northlake Characterization Core");
const KESTREL = org(2, "kestrel", "Kestrel XRD Services");
const AURORA = org(3, "aurora", "Aurora Beamline");
const QUARTZ = org(6, "quartz", "Quartz Spectroscopy");

const OPEN: InlineQuestionItem = {
  id: "esc:1",
  at: "2026-11-02T10:00:00Z",
  type: "inline_question",
  escalation_id: "esc-northlake",
  thread_id: "thread-northlake",
  question: "What is the project's budget?",
  from_org: org(5, "ridgeline", "Ridgeline Fabrication"),
  status: "open",
  action: null,
  answer: null,
  askers: [],
  recipients: [],
  deliveries: [],
  answered_by: null,
  answered_at: null,
};

function asker(from: OrgRef, question: string): QuestionAsker {
  return { escalation_id: `esc-${from.slug}`, thread_id: `thread-${from.slug}`, from_org: from, question, status: "open" };
}

const GROUPED: InlineQuestionItem = {
  ...OPEN,
  question: "Northlake asks whether this is one batch of 12 powders or the first of several rounds.",
  from_org: NORTHLAKE,
  askers: [
    asker(KESTREL, "Will more batches follow, and roughly when?"),
    asker(AURORA, "Will there be later rounds? Should we hold beam time in January?"),
  ],
  recipients: [NORTHLAKE, KESTREL, AURORA, QUARTZ],
};

function show(item: Partial<InlineQuestionItem>, onAnswer: (body: ResolveEscalation) => void = () => undefined): void {
  cleanup();
  render(
    <MemoryRouter>
      <InlineQuestionCard
        item={{ ...OPEN, ...item }}
        us="Henderson Lab"
        busy={false}
        blocked={null}
        error={null}
        onAnswer={onAnswer}
      />
    </MemoryRouter>,
  );
}

it("leads with a long question's first paragraph and sets off each paragraph's label", () => {
  show({
    question:
      "Northlake replied with a quote. Please advise what to reply.\n\nSchedule: XRD is booked until Nov 10.\n\nThey ask us to confirm 12 samples. Can we?",
  });
  const lead = screen.getByText("Northlake replied with a quote. Please advise what to reply.");
  expect(lead.className).toContain("text-ui-title");
  const label = screen.getByText("Schedule:");
  expect(label.className).toContain("font-medium");
  expect(label.parentElement?.textContent).toBe("Schedule: XRD is booked until Nov 10.");
  // A paragraph that opens with a sentence has no label.
  expect(screen.getByText("They ask us to confirm 12 samples. Can we?").children).toHaveLength(0);
});

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

it("answers a question three conversations asked once, holds an answer naming another, and shows where it went", () => {
  const onAnswer = vi.fn();
  show(GROUPED, onAnswer);
  expect(screen.getByText("Same question from 3")).toBeTruthy();
  expect(screen.getByText(/Should we hold beam time in January\?/)).toBeTruthy();
  // Only the askers get the answer, each other one ticked until the member unticks it.
  expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
  const goesTo = (names: string) => screen.getByText((_, node) => node?.textContent === `Answer goes to ${names}`);
  expect(goesTo("Northlake Characterization Core, Kestrel XRD Services and Aurora Beamline")).toBeTruthy();
  const boxes = screen.getAllByRole("checkbox");
  expect(boxes.map((box) => (box as HTMLInputElement).checked)).toEqual([true, true]);
  fireEvent.click(screen.getByRole("checkbox", { name: "Answer Aurora Beamline too" }));
  expect(goesTo("Northlake Characterization Core and Kestrel XRD Services")).toBeTruthy();

  // Northlake gets the words as written, so naming another organization on the request holds Send.
  const box = screen.getByRole("textbox", { name: "Your answer about Northlake Characterization Core" });
  const send = screen.getByRole("button", { name: "Send answer" });
  fireEvent.change(box, { target: { value: "Three rounds; Aurora beamline can hold January." } });
  expect(screen.getByRole("alert").textContent).toBe(
    "Your answer names Aurora Beamline, and Northlake gets your words as written. Leave the name out.",
  );
  expect(send).toHaveProperty("disabled", true);
  // A short name counts too, and so does an organization that didn't ask.
  fireEvent.change(box, { target: { value: "Same as we told Quartz." } });
  expect(screen.getByRole("alert").textContent).toBe(
    "Your answer names Quartz Spectroscopy, and Northlake gets your words as written. Leave the name out.",
  );

  fireEvent.change(box, { target: { value: "Three rounds: 12 now, about 8 at week 5 and 6 at week 9." } });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("Northlake gets your words as written. Kestrel gets them fitted to its question.")).toBeTruthy();
  fireEvent.click(send);
  expect(onAnswer).toHaveBeenCalledWith({
    action: "approve",
    text: "Three rounds: 12 now, about 8 at week 5 and 6 at week 9.",
    also_for: ["esc-kestrel"],
  });

  // Answered: one row per asker, and Review where it waits on a person.
  show({
    ...GROUPED,
    askers: [...GROUPED.askers, asker(QUARTZ, "How many rounds?")],
    status: "resolved",
    action: "approve",
    answer: "Three rounds.",
    answered_by: { id: "m1", slug: "maya", name: "Prof. Maya Henderson" },
    answered_at: "2026-11-02T14:06:00Z",
    deliveries: [
      { thread_id: "thread-northlake", org: NORTHLAKE, state: "as_written", at: "2026-11-02T14:06:00Z", reason: null },
      { thread_id: "thread-kestrel", org: KESTREL, state: "fitted", at: "2026-11-02T14:07:00Z", reason: null },
      { thread_id: "thread-aurora", org: AURORA, state: "not_sent", at: null, reason: null },
      { thread_id: "thread-quartz", org: QUARTZ, state: "held", at: null, reason: "It would book beam time." },
    ],
  });
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByText(/^Your answer · Maya, /)).toBeTruthy();
  expect(screen.queryByText(/^Answer goes to/)).toBeNull();
  const rows = within(screen.getByRole("list", { name: "Where your answer went" })).getAllByRole("listitem");
  expect(rows.map((row) => row.textContent)).toEqual([
    expect.stringMatching(/Northlake Characterization CoreSent as you wrote it · \d/),
    expect.stringMatching(/Kestrel XRD ServicesSent · fitted to Kestrel's question, within what Henderson Lab shares · \d/),
    expect.stringMatching(/Aurora BeamlineNot sentReview$/),
    expect.stringMatching(/Quartz SpectroscopyHeld for reviewIt would book beam time\.Review$/),
  ]);
  expect(screen.getByRole("link", { name: "Review Quartz Spectroscopy" }).getAttribute("href")).toBe("/requests/thread-quartz");
});
