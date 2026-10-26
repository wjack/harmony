import { expect, it } from "vitest";

import { ApiError, readCursor, unwrap } from "../api/client";
import { createFixtureApi, json, problem } from "./index";
import { conversations, inbox, memberDirectory, members, shellRoutes, story } from "./shell";

it("serves typed bodies, with the cursor header", async () => {
  const api = createFixtureApi(shellRoutes(), { cursor: 42 });
  const { data, response } = await api.GET("/api/members");
  expect(data).toEqual(memberDirectory);
  expect(readCursor(response)).toBe(42);
});

it("matches path templates, preferring exact paths, and passes params and bodies", async () => {
  const seen: unknown[] = [];
  const api = createFixtureApi({
    "POST /api/escalations/{escalation_id}/resolve": ({ params, body }) => {
      seen.push(params.escalation_id, body);
      return json({ ok: true });
    },
    "GET /api/threads/{thread_id}": () => problem(404, "not_found", "No thread."),
    "GET /api/threads": [],
  });
  await api.POST("/api/escalations/{escalation_id}/resolve", {
    params: { path: { escalation_id: "e 1" } },
    body: { action: "approve", text: null },
  });
  expect(seen).toEqual(["e 1", { action: "approve", text: null }]);
  expect(await unwrap(api.GET("/api/threads"))).toEqual([]);
});

it("answers unknown routes and handler errors with typed problems", async () => {
  const api = createFixtureApi({
    "GET /api/session": () => problem(401, "no_session", "No session.", { resolved_by: null }),
  });
  const missing = await api.GET("/api/inbox").then(
    () => null,
    (error: unknown) => error,
  );
  expect(missing).toBeNull(); // openapi-fetch resolves; unwrap is what throws

  const error = await unwrap(api.GET("/api/inbox")).catch((caught: unknown) => caught);
  expect(error).toBeInstanceOf(ApiError);
  expect((error as ApiError).status).toBe(404);
  expect((error as ApiError).code).toBe("not_found");

  const unauthorized = await unwrap(api.GET("/api/session")).catch((caught: unknown) => caught);
  expect((unauthorized as ApiError).problem).toEqual({ detail: "No session.", code: "no_session", resolved_by: null });
});

it("switches the acting member statefully, so the inbox follows", async () => {
  const api = createFixtureApi(shellRoutes());
  expect((await unwrap(api.GET("/api/inbox"))).needs_you).toEqual([]);
  const session = await unwrap(api.POST("/api/session/act-as", { body: { member_id: members.helen.id } }));
  expect(session.member.slug).toBe("helen");
  expect(session.epoch).toBe(2);
  expect((await unwrap(api.GET("/api/inbox"))).needs_you).toEqual(inbox.needs_you);
});

it("titles the story's chat the way the API does: its first message, cut", () => {
  const title = conversations[0]?.title ?? "";
  expect(title.length).toBeLessThanOrEqual(80);
  expect(title.endsWith("…")).toBe(true);
  expect(story.suggested_message?.startsWith(title.slice(0, -1))).toBe(true);
});
