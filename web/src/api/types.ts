/**
 * Named types for the rest of the app, all derived from the OpenAPI types, so a
 * regenerated `schema.d.ts` flows through without hand edits. Each area (chat,
 * requests, network, customize) keeps its own named types in its directory; this file
 * holds the ones the shell, the realtime layer and more than one area share.
 */
import type { ApiPaths } from "./client";
import type { components } from "./schema";

type Schemas = components["schemas"];

// --- Events (the SSE contract, from harmony.events.schemas) ------------------------

/** One SSE frame's `data`. `seq` is null for ephemeral stream events. */
export type EventEnvelope = Schemas["EventEnvelope"];
export type HarmonyEvent = EventEnvelope["event"];
export type HarmonyEventType = HarmonyEvent["type"];
export type EventOf<T extends HarmonyEventType> = Extract<HarmonyEvent, { type: T }>;

export type EntitySnapshot = EventOf<"entity.snapshot">;
export type DemoNotice = EventOf<"demo.notice">;
/** An escalation was assigned to `member_slug` at `org_slug`; `entity_id` is the escalation. */
export type DemoNeedsHuman = EventOf<"demo.needs_human">;
/** Payload-free demo cues: they drive the demo bar and toasts, never the query cache. */
export type DemoEvent = DemoNotice | DemoNeedsHuman;
export type SessionMemberChanged = EventOf<"session.member_changed">;
export type StreamEvent = EventOf<"stream.delta" | "stream.typing" | "stream.reset">;

// --- REST responses -----------------------------------------------------------------

type JsonBody<R> = R extends { content: { "application/json": infer B } } ? B : never;

/** The 200 body of `method path`. */
export type OkBody<
  P extends keyof ApiPaths,
  M extends "get" | "post" | "put",
> = ApiPaths[P] extends Record<M, { responses: { 200: infer R } }> ? JsonBody<R> : never;

export type SessionView = OkBody<"/api/session", "get">;
export type MemberView = SessionView["member"];
export type WorldView = SessionView["world"];
export type OrgRef = MemberView["org"];
export type MemberRef = Schemas["MemberRef"];
export type MemberDirectory = OkBody<"/api/members", "get">;
export type ThreadSummary = OkBody<"/api/threads", "get">[number];
export type ResetResult = OkBody<"/api/worlds/reset", "post">;

export type ConversationSummary = Schemas["ConversationSummary"];
export type Inbox = Schemas["Inbox"];
export type InboxItem = Schemas["InboxItem"];
export type DemoStory = Schemas["DemoStory"];
export type StoryStep = Schemas["StoryStep"];

/** The typed error body of every P1–P2 route (`code` says which failure it is). */
export type ApiProblem = Schemas["ApiProblem"];
export type ApiProblemCode = ApiProblem["code"];

// --- Round 2: steers, guidance and Reopen (docs/FEEDBACK_R2_PLAN.md §1.3) ----------

export type SteerView = Schemas["SteerView"];
export type GuidanceView = Schemas["GuidanceView"];
export type DeliveredGuidance = Schemas["DeliveredGuidance"];
export type RewrittenBy = Schemas["RewrittenBy"];
export type EarlierSummary = Schemas["EarlierSummary"];
export type ReopenOffer = Schemas["ReopenOffer"];
export type ReopenBlock = NonNullable<ReopenOffer["block"]>;
export type ThreadRunView = Schemas["ThreadRunView"];
export type EndReason = NonNullable<ThreadSummary["end_reason"]>;
export type DemoWhose = Schemas["DemoWhose"];
export type SteerStarted = Schemas["SteerStarted"];
export type ReopenStarted = Schemas["ReopenStarted"];
export type AddSteer = Schemas["AddSteer"];
export type ReopenRequest = Schemas["ReopenRequest"];
