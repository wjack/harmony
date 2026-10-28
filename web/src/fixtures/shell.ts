/**
 * The shell's fixtures: a session, the member directory, Recents, the inbox badge and
 * the walkthrough story, from the harmony-demo cast (docs/SCENARIO.md). Every area's
 * gallery merges its own routes over `shellRoutes()`.
 */
import type {
  ConversationSummary,
  DemoStory,
  EventEnvelope,
  Inbox,
  InboxItem,
  MemberDirectory,
  MemberView,
  OrgRef,
  ResetResult,
  SessionView,
} from "../api/types";
import type { BudgetView } from "../app/budget";
import type { FixtureRoutes } from "./index";

export const WORLD_ID = "0b1d7a3e-5c2f-4e8a-9a51-000000000001";
export const SESSION_ID = "0b1d7a3e-5c2f-4e8a-9a51-0000000000aa";
/** Every fixture timestamp is relative to this: the scenario's anchor, mid-morning. */
export const FIXTURE_NOW = "2026-10-26T10:30:00Z";
/**
 * When the fixture world was made: the start of its anchor day, so the world calendar
 * dates the fixtures as the seeds date theirs (FIXTURE_NOW on Oct 26, `daysAgo(40)` on
 * Sep 16), whatever day the gallery or a test runs.
 */
const FIXTURE_WORLD_CREATED = "2026-10-26T00:00:00Z";

function minutesAgo(minutes: number): string {
  return new Date(Date.parse(FIXTURE_NOW) - minutes * 60_000).toISOString();
}

export const orgs = {
  henderson: { id: "0b1d7a3e-0000-4000-8000-0000000000c1", slug: "henderson", name: "Henderson Lab" },
  northlake: {
    id: "0b1d7a3e-0000-4000-8000-0000000000c2",
    slug: "northlake",
    name: "Northlake Characterization Core",
  },
  ridgeline: { id: "0b1d7a3e-0000-4000-8000-0000000000c3", slug: "ridgeline", name: "Ridgeline Fabrication" },
  calder: { id: "0b1d7a3e-0000-4000-8000-0000000000c4", slug: "calder", name: "Calder Computational" },
  vantage: {
    id: "0b1d7a3e-0000-4000-8000-0000000000c5",
    slug: "vantage",
    name: "Vantage Market Intelligence",
  },
  cascade: { id: "0b1d7a3e-0000-4000-8000-0000000000c6", slug: "cascade", name: "Cascade Thin Films" },
  pacific: { id: "0b1d7a3e-0000-4000-8000-0000000000c7", slug: "pacific-beamline", name: "Pacific Beamline" },
} satisfies Record<string, OrgRef>;

function member(
  id: number,
  slug: string,
  name: string,
  title: string,
  role: MemberView["role"],
  org: OrgRef,
): MemberView {
  return { id: `0b1d7a3e-0000-4000-8000-0000000001${String(id).padStart(2, "0")}`, slug, name, title, role, org };
}

export const members = {
  alex: member(1, "alex", "Alex Chen", "PhD student", "admin", orgs.henderson),
  maya: member(2, "maya", "Prof. Maya Henderson", "Principal investigator", "admin", orgs.henderson),
  helen: member(3, "helen", "Dr. Helen Ward", "Core director", "admin", orgs.northlake),
  raj: member(4, "raj", "Raj Patel", "Staff scientist, XRD and SEM", "admin", orgs.northlake),
  dana: member(5, "dana", "Dana Okafor", "Operations lead", "admin", orgs.ridgeline),
  priya: member(6, "priya", "Priya Nair", "Principal", "admin", orgs.calder),
  jordan: member(7, "jordan", "Jordan Pike", "Analyst", "admin", orgs.vantage),
  lena: member(8, "lena", "Lena Ortiz", "Facility manager", "admin", orgs.cascade),
  sam: member(9, "sam", "Sam Whitfield", "Beamline scientist", "member", orgs.pacific),
} satisfies Record<string, MemberView>;

/**
 * `member` as a non-admin. Everyone in the demo is an admin (docs/SCENARIO.md), so tests
 * of the view-only paths make one.
 */
export function nonAdmin(member: MemberView): MemberView {
  return { ...member, role: "member" };
}

export const memberDirectory: MemberDirectory = {
  featured: [members.priya, members.alex, members.maya, members.helen, members.raj, members.dana, members.jordan],
  all: [members.lena, members.sam],
};

/** As the hosted app runs internal Claude: Opus 5.5 for featured orgs, Haiku 5.5 for the rest. */
function internalModel(actingAs: MemberView): SessionView["internal_model"] {
  const background = memberDirectory.all.some((m) => m.org.id === actingAs.org.id);
  return background
    ? { id: "claude-haiku-5-5", label: "Haiku 5.5", adaptive: true }
    : { id: "claude-opus-5-5", label: "Opus 5.5", adaptive: true };
}

export function sessionAs(actingAs: MemberView, epoch = 1, worldId = WORLD_ID): SessionView {
  return {
    session_id: SESSION_ID,
    epoch,
    lens: "desk",
    world_id: worldId,
    world: {
      id: worldId,
      scenario: "harmony-demo",
      mode: "live",
      status: "live",
      created_at: FIXTURE_WORLD_CREATED,
      ff_offset_s: 0,
    },
    member: actingAs,
    internal_model: internalModel(actingAs),
  };
}

// The API titles a chat with its first message, cut at a word boundary to 80
// characters (`domain/conversations.py` `title_from`): the first is the story's.
export const conversations: ConversationSummary[] = [
  {
    id: "0b1d7a3e-0000-4000-8000-0000000002a1",
    title: "Find a lab that can run XRD and SEM/EDS on 12 air-sensitive sodium-ion cathode…",
    last_activity_at: minutesAgo(2),
    has_pending_card: true,
    version: 4,
  },
  {
    id: "0b1d7a3e-0000-4000-8000-0000000002a2",
    title: "Glovebox transfer options for air-sensitive samples",
    last_activity_at: minutesAgo(60 * 26),
    has_pending_card: false,
    version: 2,
  },
  {
    id: "0b1d7a3e-0000-4000-8000-0000000002a3",
    title: "Coin-cell cycling schedule for November",
    last_activity_at: minutesAgo(60 * 50),
    has_pending_card: false,
    version: 6,
  },
];

function inboxItem(
  index: number,
  title: string,
  counterparty: OrgRef,
  question: string,
  reason: string,
  minutes: number,
  assignee: MemberView = members.helen,
): InboxItem {
  return {
    thread_id: `0b1d7a3e-0000-4000-8000-0000000003b${index}`,
    title,
    conversation_id: null,
    side: "responder",
    counterparty,
    thread_state: "awaiting_principal",
    side_to_move: "responder",
    escalation: {
      id: `0b1d7a3e-0000-4000-8000-0000000003e${index}`,
      kind: "consult",
      status: "open",
      question,
      assignees: [{ id: assignee.id, slug: assignee.slug, name: assignee.name }],
      reason,
      created_at: minutesAgo(minutes),
      version: 1,
    },
    shared_by: null,
    shared_question: null,
    access_reasons: ["routed"],
    last_activity_at: minutesAgo(minutes),
  };
}

export const inbox: Inbox = {
  needs_you: [
    inboxItem(
      1,
      "XRD and SEM/EDS for 12 air-sensitive powders",
      orgs.henderson,
      "Can you run XRD before Nov 6?",
      "The draft uses the reserve block on Wed Nov 4, which isn't in what Northlake shares, so a person decides.",
      3,
    ),
    inboxItem(
      2,
      "Thursday SEM availability",
      orgs.vantage,
      "Which clients hold your Thursday SEM slots?",
      "The question asks about other clients' bookings, which Northlake doesn't share.",
      41,
    ),
  ],
  with_your_team: [],
  claude_handling: [],
  shared_with_you: [],
  done: [],
};

/**
 * Alex's inbox while Ridgeline's question waits on him: his side's representative asked
 * it, so he answers it inline in the chat that sent the request (§7).
 */
export const alexInbox: Inbox = {
  ...inbox,
  needs_you: [
    {
      ...inboxItem(
        4,
        "XRD and SEM for 12 Na-ion cathode powders",
        orgs.ridgeline,
        "Which seal material does the holder need?",
        "Nothing on file names a seal material.",
        1,
        members.alex,
      ),
      side: "requester",
      conversation_id: "0b1d7a3e-0000-4000-8000-0000000002a1",
    },
  ],
};

export const story: DemoStory = {
  scenario: "harmony-demo",
  world_date: "2026-10-26",
  acting_as: { id: members.alex.id, slug: members.alex.slug, name: members.alex.name },
  suggested_message:
    "Find a lab that can run XRD and SEM/EDS on 12 air-sensitive sodium-ion cathode powders by Nov 6, and get an airtight sample holder made for the transfer. Budget is about $2,000.",
  suggested_recipients: [orgs.northlake, orgs.ridgeline, orgs.calder],
  steps: [],
};

function sessionBudget(spentUsd: number, capUsd = 10): NonNullable<BudgetView["session"]> {
  return { cap_usd: capUsd, spent_usd: spentUsd, reserved_usd: 0, remaining_usd: Math.max(0, capUsd - spentUsd) };
}

/** `GET /api/budget` in each state the demo bar shows (LIVE_DEMO_PLAN 3.5). */
export const budgets = {
  ok: { live: true, state: "ok", session: sessionBudget(2.6) },
  sessionExhausted: { live: true, state: "session_exhausted", session: sessionBudget(9.99) },
  dailyExhausted: {
    live: true,
    state: "daily_exhausted",
    session: sessionBudget(1.2),
    resets_at: "2026-10-27T00:00:00Z",
  },
  /** The demo's global cap is spent; this session's own cap isn't. */
  demoExhausted: { live: true, state: "demo_exhausted", session: sessionBudget(3.1) },
  /** The provider refused (billing or usage limit): even a session that spent nothing waits. */
  providerBlocked: { live: true, state: "demo_exhausted", session: sessionBudget(0) },
  paused: { live: true, state: "paused", session: sessionBudget(2.6) },
  /** Hosted caps are off (local development). */
  off: { live: false, state: "ok" },
} satisfies Record<string, BudgetView>;

/**
 * The shell's routes, acting as `actingAs`. Act-as and reset are stateful, so the
 * gallery's account menu really switches (and the dispatcher really resets).
 */
export function shellRoutes(actingAs: MemberView = members.alex): FixtureRoutes {
  let session = sessionAs(actingAs);
  const everyone = [...memberDirectory.featured, ...memberDirectory.all];
  return {
    "GET /api/session": () => session,
    "POST /api/session/start": () => session,
    "POST /api/session/act-as": ({ body }) => {
      const target = everyone.find(
        (candidate) => typeof body === "object" && body !== null && "member_id" in body && candidate.id === body.member_id,
      );
      session = sessionAs(target ?? session.member, session.epoch + 1);
      return session;
    },
    "POST /api/worlds/reset": (): ResetResult => {
      // Reset starts the story over, as its member.
      session = sessionAs(members.alex, session.epoch + 1);
      return { session, previous_world_id: WORLD_ID, previous_world_status: "retiring" };
    },
    "GET /api/members": memberDirectory,
    "GET /api/conversations": () => (session.member.slug === "alex" ? conversations : []),
    "GET /api/inbox": () =>
      session.member.slug === "helen" ? inbox : { ...inbox, needs_you: [] },
    "GET /api/demo/story": story,
    "GET /api/budget": budgets.ok,
    "GET /api/demo/waiting": { accounts: [] },
  };
}

/** A `demo.needs_human` cue as the stream delivers it (to emit in a gallery section). */
export function needsHumanEnvelope(seq: number, memberSlug: string, escalationId: string, orgSlug = "northlake"): EventEnvelope {
  return {
    world_id: WORLD_ID,
    seq,
    created_at: FIXTURE_NOW,
    scope: { kind: "demo", org_id: null, member_id: null, thread_id: null, session_id: null },
    event: {
      type: "demo.needs_human",
      kind: "needs_human",
      org_slug: orgSlug,
      member_slug: memberSlug,
      entity_id: escalationId,
    },
  };
}
