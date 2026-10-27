import { describe, expect, it } from "vitest";

import { unwrap } from "../api/client";
import { problemOf } from "../requests/api";
import { createFixtureApi } from "./index";
import {
  ESCALATION,
  HENDERSON_SHORT_QUESTION,
  HENDERSON_WHY,
  THREAD,
  inboxFor,
  requestsRoutes,
  requestsWorld,
  routedToBoth,
  stopped,
} from "./requests";
import { members } from "./shell";

const NOW = Date.parse("2026-10-26T10:30:00Z");

function sections(threadIds: { thread_id: string }[]): string[] {
  return threadIds.map((item) => item.thread_id);
}

const henderson = { params: { path: { thread_id: THREAD.henderson } } };

describe("inboxFor", () => {
  it("sorts Helen's inbox into the four sections", () => {
    const inbox = inboxFor(requestsWorld(NOW), members.helen);
    // Oldest first: Vantage's held reply has waited longer than Henderson's question.
    expect(sections(inbox.needs_you)).toEqual([THREAD.vantage, THREAD.henderson]);
    expect(sections(inbox.claude_handling)).toEqual([THREAD.pacific]);
    expect(sections(inbox.shared_with_you)).toEqual([THREAD.calder]);
    expect(sections(inbox.done)).toEqual([THREAD.ridgeline, THREAD.survey]);
  });

  it("carries each item's question and why it needs a person", () => {
    const [held, question] = inboxFor(requestsWorld(NOW), members.helen).needs_you;
    expect(question?.escalation).toMatchObject({ kind: "consult", reason: expect.stringContaining("reserve") as unknown });
    // A held message reads the way `api/routes/inbox.py` writes it.
    expect(held?.escalation).toMatchObject({
      kind: "outbound_review",
      question: "Review the outgoing message",
      reason: "A teammate approves every message in Ask mode.",
    });
  });

  it("carries the answerer's short question, and why the system stopped a thread", () => {
    const inbox = inboxFor(stopped(requestsWorld(NOW), THREAD.pacific, "demo_budget"), members.helen);
    const question = inbox.needs_you.find((item) => item.thread_id === THREAD.henderson);
    expect(question?.escalation?.short_question).toBe(HENDERSON_SHORT_QUESTION);
    expect(question?.stop_reason).toBeNull();
    // A held message has no question of its own to shorten.
    expect(inbox.needs_you.find((item) => item.thread_id === THREAD.vantage)?.escalation?.short_question).toBeNull();
    expect(inbox.done.find((item) => item.thread_id === THREAD.pacific)).toMatchObject({
      thread_state: "closed",
      stop_reason: "demo_budget",
    });
  });

  it("routes Henderson's item to Helen and Raj, each with their own why", () => {
    const world = routedToBoth(requestsWorld(NOW));
    expect(sections(inboxFor(world, members.raj).needs_you)).toEqual([THREAD.henderson]);
    const route = world.threads.get(THREAD.henderson)?.workspace.escalations[0]?.drafts[0]?.route;
    expect(route?.assignees).toEqual([
      { slug: "helen", why: HENDERSON_WHY.helen },
      { slug: "raj", why: HENDERSON_WHY.raj },
    ]);
  });

  it("shows Raj only what was routed or shared to him", () => {
    const inbox = inboxFor(requestsWorld(NOW), members.raj);
    expect(inbox.needs_you).toEqual([]);
    expect(sections(inbox.shared_with_you)).toEqual([THREAD.henderson]);
    expect(sections(inbox.claude_handling)).toEqual([THREAD.calder]);
    expect(inbox.shared_with_you[0]?.shared_by?.slug).toBe("helen");
  });
});

describe("the world", () => {
  it("flags Henderson's draft by the sharing check, not the answerer", () => {
    const record = requestsWorld(NOW).threads.get(THREAD.henderson);
    const consult = record?.workspace.consults[0];
    expect(consult).toMatchObject({ needs_human: false, sharing_check: { within_sharing: false } });
    expect(record?.workspace.escalations[0]?.reason).toBe(consult?.sharing_check?.reason);
    expect(record?.workspace.escalations[0]?.drafts[0]?.activity.map((line) => line.tool)).toContain("read_document");
  });

  it("gives each representative its frozen prompt, with a trusted organization's extra", () => {
    const world = requestsWorld(NOW);
    const labels = (threadId: string) =>
      world.threads.get(threadId)?.workspace.representative?.parts.map((part) => part.label);
    expect(labels(THREAD.henderson)).toEqual([
      "Who it's talking to",
      "Public summary",
      "What it may pass on",
      "Instructions",
    ]);
    expect(labels(THREAD.pacific)).toContain("Also for Pacific Beamline");
    expect(world.threads.get(THREAD.pacific)?.workspace.representative?.released).toHaveLength(1);
  });

  it("keeps a closed thread's summary on our side", () => {
    const record = requestsWorld(NOW).threads.get(THREAD.ridgeline);
    expect(record?.detail.state).toBe("closed");
    expect(record?.detail.messages.at(-1)).toMatchObject({ kind: "close", body: "" });
    expect(record?.workspace.close_summary).toContain("Ridgeline");
  });
});

describe("requestsRoutes", () => {
  it("approving the Henderson draft releases it to this thread and leaves the conversation with Claude", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const resolved = await unwrap(
      api.POST("/api/escalations/{escalation_id}/resolve", {
        params: { path: { escalation_id: ESCALATION.henderson } },
        body: { action: "approve", text: "Yes, Wednesday.", draft_rev: 1 },
      }),
    );
    // Like the server: a released consult sends no message itself.
    expect(resolved).toMatchObject({ action: "approve", message_id: null });

    // Like api/routes/inbox.py: the live thread is Claude's again, showing what was resolved.
    const inbox = await unwrap(api.GET("/api/inbox"));
    expect(sections(inbox.needs_you)).not.toContain(THREAD.henderson);
    expect(sections(inbox.done)).not.toContain(THREAD.henderson);
    expect(inbox.claude_handling.find((item) => item.thread_id === THREAD.henderson)?.escalation).toMatchObject({
      id: ESCALATION.henderson,
      status: "resolved",
    });

    const detail = await unwrap(api.GET("/api/threads/{thread_id}", henderson));
    expect(detail.state).toBe("active");
    expect(detail.messages.at(-1)).toMatchObject({ provenance: "agent_approved", body: "Yes, Wednesday." });
    const workspace = await unwrap(api.GET("/api/threads/{thread_id}/workspace", henderson));
    expect(workspace.consults[0]).toMatchObject({
      status: "released",
      released_text: "Yes, Wednesday.",
      released_by: "member",
      released_by_member: { slug: "helen" },
    });
    expect(workspace.representative?.released.map((answer) => answer.text)).toEqual(["Yes, Wednesday."]);
  });

  it("releases the drafted answer when the approval carries no text", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    await unwrap(
      api.POST("/api/escalations/{escalation_id}/resolve", {
        params: { path: { escalation_id: ESCALATION.henderson } },
        body: { action: "approve", draft_rev: 1 },
      }),
    );
    const workspace = await unwrap(api.GET("/api/threads/{thread_id}/workspace", henderson));
    expect(workspace.consults[0]?.released_text).toBe(workspace.escalations[0]?.drafts[0]?.answer);
  });

  it("sends an approved held message, and sends nothing for a declined one", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const params = { params: { path: { thread_id: THREAD.vantage } } };
    const before = (await unwrap(api.GET("/api/threads/{thread_id}", params))).messages.length;
    const resolved = await unwrap(
      api.POST("/api/escalations/{escalation_id}/resolve", {
        params: { path: { escalation_id: ESCALATION.vantage } },
        body: { action: "decline", text: "Too much detail.", draft_rev: 1 },
      }),
    );
    expect(resolved.message_id).toBeNull();
    const detail = await unwrap(api.GET("/api/threads/{thread_id}", params));
    expect(detail.messages).toHaveLength(before);

    const world = requestsWorld(NOW);
    const fresh = createFixtureApi(requestsRoutes({ world }));
    const sent = await unwrap(
      fresh.POST("/api/escalations/{escalation_id}/resolve", {
        params: { path: { escalation_id: ESCALATION.vantage } },
        body: { action: "approve", draft_rev: 1 },
      }),
    );
    const messages = world.threads.get(THREAD.vantage)?.detail.messages ?? [];
    expect(messages.at(-1)).toMatchObject({ id: sent.message_id, provenance: "agent_approved" });
  });

  it("sends nothing for a held message sent back to be revised, and leaves the turn with Claude", async () => {
    const world = requestsWorld(NOW);
    const api = createFixtureApi(requestsRoutes({ world }));
    const before = world.threads.get(THREAD.vantage)?.detail.messages.length;
    const resolved = await unwrap(
      api.POST("/api/escalations/{escalation_id}/resolve", {
        params: { path: { escalation_id: ESCALATION.vantage } },
        body: { action: "revise", text: "Point them to the public price list.", draft_rev: 1 },
      }),
    );
    expect(resolved).toMatchObject({ action: "revise", message_id: null });
    const record = world.threads.get(THREAD.vantage);
    expect(record?.detail.messages).toHaveLength(before ?? 0);
    expect(record?.detail.state).toBe("active");
    expect(record?.workspace.escalations[0]).toMatchObject({ status: "resolved", resolution: { action: "revise" } });
    // An instruction to the representative, never words it may pass on.
    expect(record?.workspace.representative?.released).toEqual([]);
  });

  it("says what the budget is doing", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW), budget: "paused" }));
    expect(await unwrap(api.GET("/api/budget"))).toEqual({ live: false, state: "paused" });
  });

  it("answers a second resolve with who got there first", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const resolve = () =>
      unwrap(
        api.POST("/api/escalations/{escalation_id}/resolve", {
          params: { path: { escalation_id: ESCALATION.vantage } },
          body: { action: "decline", text: "No." },
        }),
      );
    await resolve();
    const error = await resolve().catch((caught: unknown) => caught);
    expect(problemOf(error)).toMatchObject({ code: "escalation_not_open", resolved_by: { slug: "helen" } });
  });

  it("moves a reassigned item and its thread access, recording nothing else", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const reassigned = await unwrap(
      api.POST("/api/escalations/{escalation_id}/reassign", {
        params: { path: { escalation_id: ESCALATION.vantage } },
        body: { member_id: members.raj.id },
      }),
    );
    expect(reassigned.assignees).toEqual([members.raj.id]);
    const params = { params: { path: { thread_id: THREAD.vantage } } };
    const workspace = await unwrap(api.GET("/api/threads/{thread_id}/workspace", params));
    expect(workspace.escalations[0]?.assignees.map((assignee) => assignee.slug)).toEqual(["raj"]);
    const detail = await unwrap(api.GET("/api/threads/{thread_id}", params));
    expect(detail.access.some((row) => row.member_slug === "raj" && row.reason === "routed")).toBe(true);
  });

  it("refuses to reassign outside the org", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const error = await unwrap(
      api.POST("/api/escalations/{escalation_id}/reassign", {
        params: { path: { escalation_id: ESCALATION.henderson } },
        body: { member_id: members.alex.id },
      }),
    ).catch((caught: unknown) => caught);
    expect(problemOf(error)?.code).toBe("not_in_org");
  });

  it("lands a redraft as the next rev, and only for a consult", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const started = await unwrap(
      api.POST("/api/escalations/{escalation_id}/redraft", {
        params: { path: { escalation_id: ESCALATION.henderson } },
        body: { instruction: "Shorter." },
      }),
    );
    expect(started.draft_rev).toBe(2);
    const workspace = await unwrap(api.GET("/api/threads/{thread_id}/workspace", henderson));
    expect(workspace.escalations[0]).toMatchObject({ current_draft_rev: 2 });
    expect(workspace.escalations[0]?.drafts.map((draft) => draft.draft_rev)).toEqual([1, 2]);

    const error = await unwrap(
      api.POST("/api/escalations/{escalation_id}/redraft", {
        params: { path: { escalation_id: ESCALATION.vantage } },
        body: { instruction: "Shorter." },
      }),
    ).catch((caught: unknown) => caught);
    expect(problemOf(error)).toMatchObject({ code: "escalation_not_open", resolved_by: null });
  });

  it("shares a thread when a note mentions a teammate", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const params = { params: { path: { thread_id: THREAD.vantage } } };
    const note = await unwrap(api.POST("/api/threads/{thread_id}/notes", { ...params, body: { body: "@raj seen them?" } }));
    expect(note.mentions.map((member) => member.slug)).toEqual(["raj"]);
    const detail = await unwrap(api.GET("/api/threads/{thread_id}", params));
    expect(detail.access.find((row) => row.member_slug === "raj")?.reason).toBe("shared");
  });

  it("hides threads the acting member can't see", async () => {
    const api = createFixtureApi(requestsRoutes({ as: members.raj, world: requestsWorld(NOW) }));
    const { response } = await api.GET("/api/threads/{thread_id}", {
      params: { path: { thread_id: THREAD.vantage } },
    });
    expect(response.status).toBe(404);
  });
});
