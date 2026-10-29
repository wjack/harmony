import { describe, expect, it } from "vitest";

import { unwrap } from "../api/client";
import { problemOf } from "../requests/api";
import { createFixtureApi } from "./index";
import {
  ESCALATION,
  HENDERSON_REWRITE,
  HENDERSON_SHORT_QUESTION,
  REOPEN_INSTRUCTION,
  THREAD,
  expired,
  inboxFor,
  reopenOffer,
  requestsRoutes,
  requestsWorld,
  stopped,
  type RequestsRouteOptions,
} from "./requests";
import { members, nonAdmin } from "./shell";

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
    expect(question?.escalation).toMatchObject({ kind: "consult", reason: expect.stringContaining("another client") as unknown });
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

  it("shows Raj, as a member, only what was routed or shared to him", () => {
    const inbox = inboxFor(requestsWorld(NOW), nonAdmin(members.raj));
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

  it("keeps a closed thread's summary on our side, and records who closed the run", () => {
    const record = requestsWorld(NOW).threads.get(THREAD.ridgeline);
    expect(record?.detail).toMatchObject({ state: "closed", end_reason: "closed", closed_by: "requester" });
    expect(record?.detail.runs[0]?.ended).toMatchObject({ end_reason: "closed", closed_by: "requester" });
    expect(record?.detail.reopen).toEqual({ state: "allowed", block: null });
    expect(record?.detail.messages.at(-1)).toMatchObject({ kind: "close", body: "" });
    expect(record?.workspace.close_summary).toContain("Ridgeline");
  });

  it("ends a run without a close for a reason, and keeps seeded history from being reopened", () => {
    const world = expired(requestsWorld(NOW), THREAD.pacific, "turn_limit");
    expect(world.threads.get(THREAD.pacific)?.detail).toMatchObject({ state: "expired", end_reason: "turn_limit" });
    expect(world.threads.get(THREAD.survey)?.detail.reopen).toEqual({ state: "blocked", block: "history" });
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

  it("shares a thread when a note mentions a teammate", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const params = { params: { path: { thread_id: THREAD.vantage } } };
    const body = { body: "@raj seen them?", as_member_id: members.helen.id };
    const note = await unwrap(api.POST("/api/threads/{thread_id}/notes", { ...params, body }));
    expect(note.mentions.map((member) => member.slug)).toEqual(["raj"]);
    const detail = await unwrap(api.GET("/api/threads/{thread_id}", params));
    expect(detail.access.find((row) => row.member_slug === "raj")?.reason).toBe("shared");
  });

  it("refuses a comment from a member the tab no longer acts as", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const params = { params: { path: { thread_id: THREAD.vantage } } };
    const error = await unwrap(
      api.POST("/api/threads/{thread_id}/notes", { ...params, body: { body: "Seen them?", as_member_id: members.raj.id } }),
    ).catch((caught: unknown) => caught);
    expect(problemOf(error)).toMatchObject({ code: "member_changed", member: { slug: "helen" } });
  });

  it("hides threads the acting member can't see", async () => {
    const api = createFixtureApi(requestsRoutes({ as: nonAdmin(members.raj), world: requestsWorld(NOW) }));
    const { response } = await api.GET("/api/threads/{thread_id}", {
      params: { path: { thread_id: THREAD.vantage } },
    });
    expect(response.status).toBe(404);
  });
});

describe("Edit (POST /steers)", () => {
  const routes = (options: RequestsRouteOptions = {}) =>
    createFixtureApi(requestsRoutes({ world: requestsWorld(NOW), ...options }));
  const tell = (api: ReturnType<typeof routes>, threadId: string, fields: { client_key: string; as_member_id?: string }) =>
    unwrap(
      api.POST("/api/threads/{thread_id}/steers", {
        params: { path: { thread_id: threadId } },
        body: {
          as_member_id: members.helen.id,
          body: "Keep it shorter.",
          escalation_id: threadId === THREAD.henderson ? ESCALATION.henderson : null,
          ...fields,
        },
      }),
    );

  it("lands at once: guidance updated, and the open item rewritten as its next rev", async () => {
    const api = routes();
    expect(await tell(api, THREAD.henderson, { client_key: "k1" })).toMatchObject({
      escalation_id: ESCALATION.henderson,
      draft_rev: 2,
    });
    const workspace = await unwrap(api.GET("/api/threads/{thread_id}/workspace", henderson));
    expect(workspace.steers).toMatchObject([
      { kind: "edit", status: "done", guidance_outcome: "updated", draft_outcome: "rewritten", draft_rev: 2 },
    ]);
    expect(workspace.guidance).toMatchObject({ version: 1, text: "Keep it shorter.", by: { slug: "helen" } });
    expect(workspace.escalations[0]?.drafts.at(-1)).toMatchObject({
      draft_rev: 2,
      answer: HENDERSON_REWRITE,
      rewritten_by: { author: { slug: "helen" } },
    });
  });

  it("gives a retried POST the same steer back", async () => {
    const api = routes();
    const first = await tell(api, THREAD.henderson, { client_key: "k1" });
    const again = await tell(api, THREAD.henderson, { client_key: "k1" });
    expect(again).toEqual(first);
    const workspace = await unwrap(api.GET("/api/threads/{thread_id}/workspace", henderson));
    expect(workspace.steers).toHaveLength(1);
  });

  it("refuses a second Edit while one runs, naming whose it is", async () => {
    const api = routes({ steerHangs: true });
    await tell(api, THREAD.henderson, { client_key: "k1" });
    const error = await tell(api, THREAD.henderson, { client_key: "k2" }).catch((caught: unknown) => caught);
    expect(problemOf(error)).toMatchObject({ code: "steer_running", by: { slug: "helen" } });
  });

  it("refuses an Edit on a closed thread, and from a member the tab no longer acts as", async () => {
    const api = routes();
    const closed = await tell(api, THREAD.ridgeline, { client_key: "k1" }).catch((caught: unknown) => caught);
    expect(problemOf(closed)?.code).toBe("thread_closed");
    const switched = await tell(api, THREAD.henderson, { client_key: "k2", as_member_id: members.raj.id }).catch(
      (caught: unknown) => caught,
    );
    expect(problemOf(switched)).toMatchObject({ code: "member_changed", member: { slug: "helen" } });
  });
});

describe("Reopen (POST /reopen)", () => {
  const ridgeline = { params: { path: { thread_id: THREAD.ridgeline } } };
  const reopen = (api: ReturnType<typeof createFixtureApi>, threadId = THREAD.ridgeline, key = "k1") =>
    unwrap(
      api.POST("/api/threads/{thread_id}/reopen", {
        params: { path: { thread_id: threadId } },
        body: { client_key: key, as_member_id: members.helen.id, body: REOPEN_INSTRUCTION },
      }),
    );

  it("starts a reopen attempt that changes nothing shared until it settles", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW) }));
    const started = await reopen(api);
    expect(started.thread_id).toBe(THREAD.ridgeline);
    expect(await reopen(api)).toEqual(started);
    const workspace = await unwrap(api.GET("/api/threads/{thread_id}/workspace", ridgeline));
    expect(workspace.steers).toMatchObject([{ id: started.steer_id, kind: "reopen", status: "working", body: REOPEN_INSTRUCTION }]);
    expect((await unwrap(api.GET("/api/threads/{thread_id}", ridgeline))).state).toBe("closed");
    const second = await reopen(api, THREAD.ridgeline, "k2").catch((caught: unknown) => caught);
    expect(problemOf(second)).toMatchObject({ code: "reopen_in_progress", reopened_by: { member: { slug: "helen" } } });
  });

  it("opens a new run with Claude's next message, archiving the first run's summary", async () => {
    const world = requestsWorld(NOW);
    const summary = world.threads.get(THREAD.ridgeline)?.workspace.close_summary;
    const api = createFixtureApi(requestsRoutes({ world, reopenSettles: "opened" }));
    await reopen(api);
    const detail = await unwrap(api.GET("/api/threads/{thread_id}", ridgeline));
    expect(detail).toMatchObject({ state: "active", run: 2, reopen: null });
    expect(detail.runs.at(-1)?.reopened).toMatchObject({ by: { slug: "helen" } });
    expect(detail.messages.at(-1)?.body).toContain("general capabilities sheet");
    const workspace = await unwrap(api.GET("/api/threads/{thread_id}/workspace", ridgeline));
    expect(workspace.earlier_summaries).toEqual([{ run: 1, text: summary }]);
    expect(workspace.close_summary).toBeNull();
    expect(workspace.guidance?.text).toBe(REOPEN_INSTRUCTION);
  });

  it("records a failed attempt privately, leaving the thread closed", async () => {
    const api = createFixtureApi(requestsRoutes({ world: requestsWorld(NOW), reopenSettles: "unchanged" }));
    await reopen(api);
    const workspace = await unwrap(api.GET("/api/threads/{thread_id}/workspace", ridgeline));
    expect(workspace.steers[0]).toMatchObject({ status: "failed", failure_reason: "unchanged" });
    expect((await unwrap(api.GET("/api/threads/{thread_id}", ridgeline))).state).toBe("closed");
  });

  it("refuses an open thread, one still wrapping up, and seeded history", async () => {
    const world = reopenOffer(requestsWorld(NOW), THREAD.ridgeline, { state: "wrapping_up", block: null });
    const api = createFixtureApi(requestsRoutes({ world }));
    const open = await reopen(api, THREAD.henderson).catch((caught: unknown) => caught);
    expect(problemOf(open)?.code).toBe("thread_open");
    const wrapping = await reopen(api).catch((caught: unknown) => caught);
    expect(problemOf(wrapping)?.code).toBe("closing_in_progress");
    const history = await reopen(api, THREAD.survey).catch((caught: unknown) => caught);
    expect(problemOf(history)).toMatchObject({ code: "not_reopenable", block: "history" });
  });
});
