/**
 * The Requests inbox's fixtures: Northlake's side of the harmony-demo story
 * (docs/REVISION_UNSTRUCTURED.md §10). Helen's inbox holds Henderson Frame Lab's CT request,
 * where Claude's draft says which client freed the Nov 4 block (from Raj's note) and the
 * sharing check stops it, and a reply to Vantage held for review because Northlake
 * trusts Vantage on Ask.
 * Beside them sit threads Claude is handling and ones already closed.
 *
 * The routes are a small in-memory server: resolving, reassigning, Edits, Reopen,
 * comments and sharing change the world, so the gallery's buttons really move items
 * between sections. Timestamps are relative to `now`, so ages read as they would in the app.
 */
import type {
  AddSteer,
  EndReason,
  GuidanceView,
  ReopenBlock,
  ReopenOffer,
  ReopenRequest,
  SteerView,
} from "../api/types";
import type { BudgetState, BudgetView } from "../app/budget";
import { mentionedSlugs } from "../requests/mentions";
import { customizeRoutes, northlakeSettings } from "./customize";
import { problem, type FixtureRequest, type FixtureRoutes } from "./index";
import { members, orgs, sessionAs, shellRoutes } from "./shell";
import type {
  ActivityLine,
  AnswerUsed,
  ConsultView,
  ContextPart,
  EscalationDraft,
  EscalationResolved,
  EscalationView,
  Inbox,
  InboxItem,
  MemberRef,
  MemberView,
  NoteView,
  OrgRef,
  RepresentativeContext,
  ResolveEscalation,
  RetryEscalation,
  Side,
  ThreadAccess,
  ThreadDetail,
  ThreadMessage,
  ThreadState,
  ThreadWorkspace,
} from "../requests/types";

function id(suffix: string): string {
  return `0b1d7a3e-0000-4000-8000-${suffix.padStart(12, "0")}`;
}

export const THREAD = {
  henderson: id("9a1"),
  vantage: id("9a2"),
  pacific: id("9a4"),
  calder: id("9a5"),
  ridgeline: id("9a6"),
  survey: id("9a7"),
} as const;

export const ESCALATION = {
  henderson: id("9e1"),
  vantage: id("9e2"),
  ridgeline: id("9e6"),
  survey: id("9e7"),
} as const;

const CONSULT = { henderson: id("9c1"), pacific: id("9c4"), ridgeline: id("9c6") } as const;

function ref(member: MemberView): MemberRef {
  return { id: member.id, slug: member.slug, name: member.name };
}

const helen = ref(members.helen);
const raj = ref(members.raj);
const northlake: OrgRef = orgs.northlake;

// --- Server-written text ----------------------------------------------------------------

/** `api/routes/inbox.py`: how a held message (`outbound_review`) reads in the inbox. */
const HELD_QUESTION = "Review the outgoing message";
// A turn Claude declined (`refused_turn`), as the inbox and the workspace word it.
const REFUSED_TURN_QUESTION = "Claude declined to continue the conversation";
const REFUSED_TURN_REASON = "Retry Claude's turn, or end the conversation.";
const HELD_REASON = "A teammate approves every message in Ask mode.";

/** What the representative is told after a teammate replied directly (`take_over`). */
const TAKEN_OVER = "A member of your side wrote to the counterparty directly.";

/**
 * `agents/consult.py`: a settled item's result as the representative receives it, with a
 * person's words quoted when there are any (`result_text`, `held_result`).
 */
function settled(said: string, label: "What you may say" | "What they sent", words: string): string {
  return words === "" ? said : `${said} ${label}:\n<released_text>\n${words}\n</released_text>`;
}

/** `domain/conversations.py`'s labels for the answerer's tool calls. */
function searched(query: string): ActivityLine {
  return { tool: "search_org_knowledge", label: "Searched your organization's knowledge", detail: query };
}

/** `read_document`'s line names the record it read by its title. */
function read(title: string): ActivityLine {
  return { tool: "read_document", label: "Read a document", detail: title };
}

// --- Drafts ------------------------------------------------------------------------------

export const HENDERSON_QUESTION =
  "Can we run CT and compression tests on 12 lattice coupons before Nov 6, and at what price?";

/** The answerer's short form of the question, the decision card's title. */
export const HENDERSON_SHORT_QUESTION =
  "Can we run all 12 by Nov 6, at what price, and with CT before the compression tests?";

const HENDERSON_ANSWER =
  "Yes. Another client released our Wednesday, Nov 4 compression block on Friday, so we can scan your coupons that morning and test them in the block that afternoon. Compression reports would be ready by Thursday, Nov 5, and CT reports by Friday, Nov 6.\n\n" +
  "At our published academic prices that's $1,434: 12 × $45 compression and 12 × $60 CT, each 10% off for 10 or more coupons, plus $150 for intake and setup and the $150 priority fee. " +
  "The coupons need to reach our front desk by 17:00 on Tuesday, Nov 3, each engraved with its ID.";

const HENDERSON_SHARING_REASON =
  "The answer says another client released the Nov 4 compression block. Northlake shares what's open on the public calendar, not other clients or their bookings.";

/** Why routing gave Henderson's item to each member, in the answerer's words to them. */
export const HENDERSON_WHY = {
  helen: "you schedule the instruments and their bookings",
  raj: "you run the CT",
} as const;

/** Claude's first draft for Henderson (the escalation's rev 1). */
export function hendersonDraft(createdAt: string): EscalationDraft {
  return {
    draft_rev: 1,
    answer: HENDERSON_ANSWER,
    short_question: HENDERSON_SHORT_QUESTION,
    route: { assignees: [{ slug: "helen", why: HENDERSON_WHY.helen }] },
    rationale:
      "The public calendar has a priority compression block open on Wed, Nov 4, 13:00 to 17:00, enough for up to 14 coupons, and a CT session that morning; Raj's note says a client released the block on Friday. " +
      "The price is the published academic list: (12 × $45 + 12 × $60), 10% off each, plus $150 intake and the $150 priority fee = $1,434.",
    activity: [
      searched("Compression availability before Nov 6"),
      read("Public calendar"),
      read("Raj's note, Nov 4 compression block"),
      read("Price list, external academic"),
    ],
    rewritten_by: null,
    created_at: createdAt,
  };
}

/** Henderson's answer as Claude rewrites it after an Edit (the fixture can't think). */
export const HENDERSON_REWRITE =
  "Yes. We can scan your coupons on the morning of Wednesday, Nov 4 and test them in our priority compression block that afternoon, with results to you by Friday, Nov 6. " +
  "At our published academic prices that's $1,434 for all 12.\n\n" +
  "Please drop the coupons at our front desk by 17:00 on Tuesday, Nov 3.";

/** Vantage's held reply as Claude rewrites it after an Edit. */
const VANTAGE_REWRITE =
  "We don't share who books our instruments or what clients pay. " +
  "Our public calendar shows which Thursday CT slots are open, and our external academic price list is on our directory listing.";

/** What Helen tells Claude in the fixtures' Edits. */
export const EDIT_INSTRUCTION = "Keep it shorter, and give the drop-off deadline its own paragraph.";

/** What Helen types to reopen a conversation in the fixtures. */
export const REOPEN_INSTRUCTION = "Offer them our general capabilities sheet instead.";

/** A message the representative wrote in Ask mode, held as an `outbound_review` draft. */
function heldDraft(answer: string, createdAt: string): EscalationDraft {
  return {
    draft_rev: 1,
    answer,
    short_question: null,
    route: null,
    rationale: HELD_REASON,
    activity: [],
    rewritten_by: null,
    created_at: createdAt,
  };
}

export const VANTAGE_REPLY =
  "We don't share who books our instruments or what individual clients pay. " +
  "Our external academic price list and the open slots on our public calendar are on our directory listing, and we're happy to quote for work you'd like run yourselves.";

// --- Threads -------------------------------------------------------------------------

interface ThreadRecord {
  detail: Omit<ThreadDetail, "access_reason" | "access_reasons">;
  workspace: ThreadWorkspace;
}

interface Clock {
  now: number;
  at: (minutesAgo: number) => string;
}

function clock(now: number): Clock {
  return { now, at: (minutesAgo) => new Date(now - minutesAgo * 60_000).toISOString() };
}

function message(
  seq: number,
  from: OrgRef,
  provenance: ThreadMessage["provenance"],
  body: string,
  createdAt: string,
  threadKey: string,
): ThreadMessage {
  return {
    id: id(`9m${threadKey}${String(seq)}`),
    seq,
    from_org_id: from.id,
    kind: "message",
    body,
    provenance,
    created_at: createdAt,
  };
}

/** A side's `close`: an empty marker in the transcript; its summary stays with its own org. */
function closed(seq: number, from: OrgRef, createdAt: string, threadKey: string): ThreadMessage {
  return { ...message(seq, from, "agent", "", createdAt, threadKey), kind: "close" };
}

function routed(member: MemberRef): ThreadAccess {
  return {
    member_id: member.id,
    member_slug: member.slug,
    member_name: member.name,
    reason: "routed",
    shared_by_member_id: null,
    question: null,
  };
}

function shared(member: MemberRef, by: MemberRef, question: string | null): ThreadAccess {
  return {
    member_id: member.id,
    member_slug: member.slug,
    member_name: member.name,
    reason: "shared",
    shared_by_member_id: by.id,
    question,
  };
}

/**
 * The representative's frozen prompt for a thread with `counterparty`, labelled the way
 * `agents/context.py` labels it: who it's talking to, the public summary, what it may
 * pass on (plus a trusted organization's extra), and the instructions.
 */
function representative(counterparty: OrgRef, frozenAt: string): RepresentativeContext {
  const extra = northlakeSettings.trusted_organizations.find((row) => row.counterparty.id === counterparty.id);
  const parts: ContextPart[] = [
    {
      kind: "counterparty",
      label: "Who it's talking to",
      text: `${counterparty.name}, represented by its own agent, "Claude for ${counterparty.name}". Harmony verified this identity when the conversation opened.`,
    },
    { kind: "public_summary", label: "Public summary", text: northlakeSettings.public_summary },
    { kind: "sharing", label: "What it may pass on", text: northlakeSettings.sharing_description },
  ];
  if (extra !== undefined && extra.sharing_description !== "") {
    parts.push({ kind: "sharing", label: `Also for ${counterparty.name}`, text: extra.sharing_description });
  }
  parts.push({ kind: "instructions", label: "Instructions", text: northlakeSettings.instructions });
  return { parts, notices: [], released: [], guidance: [], frozen_at: frozenAt };
}

function consult(
  consultId: string,
  question: string,
  createdAt: string,
  fields: Partial<Omit<ConsultView, "id" | "question" | "created_at">>,
): ConsultView {
  return {
    id: consultId,
    question,
    status: "answering",
    answer: null,
    needs_human: null,
    needs_human_reason: null,
    sharing_check: null,
    released_text: null,
    released_by: null,
    released_by_member: null,
    released_at: null,
    version: 1,
    created_at: createdAt,
    ...fields,
  };
}

function escalation(
  escalationId: string,
  kind: EscalationView["kind"],
  consultId: string | null,
  reason: string,
  draft: EscalationDraft,
): EscalationView {
  return {
    id: escalationId,
    kind,
    status: "open",
    consult_id: consultId,
    reason,
    assignees: [helen],
    current_draft_rev: draft.draft_rev,
    drafts: [draft],
    resolution: null,
    resolved_by: null,
    resolved_at: null,
    created_at: draft.created_at,
    version: 1,
  };
}

/** `escalation`, already approved by `by` at `at`. */
function approved(open: EscalationView, by: MemberRef, at: string): EscalationView {
  return {
    ...open,
    status: "resolved",
    resolution: {
      escalation_id: open.id,
      action: "approve",
      text: null,
      draft_rev: open.current_draft_rev,
      member_id: by.id,
      org_id: northlake.id,
    },
    resolved_by: by,
    resolved_at: at,
    version: 2,
  };
}

interface ThreadParts {
  threadId: string;
  title: string;
  requester: OrgRef;
  state: ThreadState;
  createdAt: string;
  messages: ThreadMessage[];
  access: ThreadAccess[];
  consults?: ConsultView[];
  escalations?: EscalationView[];
  notes?: NoteView[];
  closeSummary?: string;
  deadline?: string;
  reopen?: ReopenOffer;
}

/** Why the system stopped a thread (a spent budget, a model error), never a person. */
export type StopReason = NonNullable<ThreadDetail["stop_reason"]>;

const ENDED: ReadonlySet<ThreadState> = new Set(["closed", "expired"]);

function thread(parts: ThreadParts): ThreadRecord {
  const notes = parts.notes ?? [];
  const consults = parts.consults ?? [];
  const context = representative(parts.requester, parts.createdAt);
  for (const asked of consults) {
    if (asked.released_text === null || asked.released_by === null || asked.released_at === null) continue;
    context.released.push({
      id: asked.id,
      question: asked.question,
      text: asked.released_text,
      released_by: asked.released_by,
      released_by_member: asked.released_by_member,
      released_at: asked.released_at,
    });
  }
  const last =
    [parts.messages.at(-1)?.created_at, notes.at(-1)?.created_at, parts.createdAt]
      .filter((at): at is string => at !== undefined)
      .sort((a, b) => Date.parse(b) - Date.parse(a))[0] ?? parts.createdAt;
  const ended = parts.state === "closed" || parts.state === "expired";
  const record: ThreadRecord = {
    detail: {
      id: parts.threadId,
      title: parts.title,
      state: parts.state,
      stop_reason: null,
      side: "responder",
      requester: parts.requester,
      responder: northlake,
      last_activity_at: last,
      run: 1,
      end_reason: null,
      ended_at: null,
      closed_by: null,
      run_finished_at: null,
      version: 1,
      side_to_move: null,
      deadline_at: parts.deadline ?? null,
      created_at: parts.createdAt,
      messages: parts.messages,
      access: parts.access,
      reopen: null,
      runs: [{ run: 1, started_at: parts.createdAt, reopened: null, ended: null }],
    },
    workspace: {
      thread_id: parts.threadId,
      side: "responder",
      consults,
      escalations: parts.escalations ?? [],
      notes,
      representative: context,
      close_summary: parts.closeSummary ?? null,
      steers: [],
      guidance: null,
      earlier_summaries: [],
      version: 1,
    },
  };
  if (ended) {
    // The requester closed it, or it went quiet: a seeded thread's two ways to end.
    endRun(record, parts.state === "closed" ? { end_reason: "closed", closed_by: "requester" } : { end_reason: "quiet" });
    if (parts.reopen !== undefined) record.detail.reopen = parts.reopen;
  }
  return record;
}

function hendersonThread({ at }: Clock): ThreadRecord {
  const henderson = orgs.henderson;
  return thread({
    threadId: THREAD.henderson,
    title: "CT and compression quote",
    requester: henderson,
    state: "awaiting_principal",
    createdAt: at(12),
    deadline: "2026-11-06T17:00:00Z",
    messages: [
      message(
        1,
        henderson,
        "agent",
        "Hello from Claude for Henderson Frame Lab, writing for Alex Chen in Prof. Maya Henderson's group.\n\n" +
          "We need CT scans and compression tests on 12 titanium lattice coupons (20 mm cubes), with results by Friday, Nov 6. " +
          "Each coupon is engraved with its ID, and Alex can drop them off at your front desk.\n\n" +
          "Can you run both before Nov 6, and what would it cost?",
        at(12),
        "1",
      ),
    ],
    access: [routed(helen), shared(raj, helen, "Can you run the priority block on Wednesday?")],
    consults: [
      consult(CONSULT.henderson, HENDERSON_QUESTION, at(11), {
        status: "needs_human",
        answer: HENDERSON_ANSWER,
        needs_human: false,
        sharing_check: { within_sharing: false, reason: HENDERSON_SHARING_REASON },
      }),
    ],
    escalations: [
      escalation(ESCALATION.henderson, "consult", CONSULT.henderson, HENDERSON_SHARING_REASON, hendersonDraft(at(10))),
    ],
    notes: [
      {
        id: id("9n11"),
        seq: 1,
        author_kind: "member",
        author: helen,
        body: "@raj can you run the priority block on Wednesday afternoon? It'd be 12 coupons, scanned that morning.",
        mentions: [raj],
        created_at: at(7),
      },
      {
        id: id("9n12"),
        seq: 2,
        author_kind: "member",
        author: raj,
        body: "Yes, 1 to 5 works if the coupons are at the front desk by 5 on Tuesday. I'll hold it until noon tomorrow.",
        mentions: [],
        created_at: at(4),
      },
    ],
  });
}

function vantageThread({ at }: Clock): ThreadRecord {
  const vantage = orgs.vantage;
  return thread({
    threadId: THREAD.vantage,
    title: "Thursday CT availability",
    requester: vantage,
    state: "awaiting_principal",
    createdAt: at(46),
    messages: [
      message(
        1,
        vantage,
        "agent",
        "Hi, this is Claude for Vantage Market Intelligence. We're mapping CT and testing capacity in the region. " +
          "Which clients currently hold your Thursday CT slots, and roughly what do they pay per hour?",
        at(46),
        "2",
      ),
    ],
    access: [routed(helen)],
    escalations: [escalation(ESCALATION.vantage, "outbound_review", null, HELD_REASON, heldDraft(VANTAGE_REPLY, at(45)))],
  });
}

function pacificThread({ at }: Clock): ThreadRecord {
  const pacific = orgs.pacific;
  const answer = "About a week from receipt; $120 per part at list price.";
  return thread({
    threadId: THREAD.pacific,
    title: "Scouting CT of printed nodes",
    requester: pacific,
    state: "active",
    createdAt: at(95),
    messages: [
      message(
        1,
        pacific,
        "agent",
        "Claude for Pacific Beamline. We'd like scouting CT scans of four printed titanium nodes before our December beamtime, to pick which go on the beamline. What's your lead time?",
        at(95),
        "4",
      ),
      message(
        2,
        northlake,
        "agent",
        "Scouting scans run about a week from receipt. Four nodes would be $480 at list price ($120 each). Do you want overview scans, high-resolution scans or both?",
        at(93),
        "4",
      ),
      message(3, pacific, "agent", "Both, please. Can you hold time the week of Nov 16?", at(18), "4"),
    ],
    access: [routed(helen)],
    consults: [
      consult(CONSULT.pacific, "What's our lead time and price for scouting CT scans of printed parts?", at(94), {
        status: "released",
        answer,
        needs_human: false,
        sharing_check: { within_sharing: true, reason: "Turnaround and list prices are part of what Northlake shares." },
        released_text: answer,
        released_by: "auto",
        released_at: at(93),
        version: 2,
      }),
    ],
  });
}

function calderThread({ at }: Clock): ThreadRecord {
  const calder = orgs.calder;
  return thread({
    threadId: THREAD.calder,
    title: "Reference compression tests for FE validation",
    requester: calder,
    state: "active",
    createdAt: at(60 * 5),
    messages: [
      message(
        1,
        calder,
        "agent",
        "Claude for Calder Computational. We need compression tests on eight reference lattice coupons to validate our finite-element models, ideally in December. Is that something you take on?",
        at(60 * 5),
        "5",
      ),
      message(
        2,
        northlake,
        "agent",
        "We do. December is open on our public calendar. I'll confirm the exact dates with the team and come back with a quote.",
        at(60 * 5 - 2),
        "5",
      ),
    ],
    access: [routed(raj), shared(helen, raj, "Can we take this on in December? It's a new client.")],
  });
}

function ridgelineThread({ at }: Clock): ThreadRecord {
  const ridgeline = orgs.ridgeline;
  const answer = "Yes: we can scan all four this week. $640 at list price ($160 each), scans within two days of receipt.";
  const draft: EscalationDraft = {
    draft_rev: 1,
    answer,
    short_question: "Can we scan four holder tubes this week, and at what price?",
    route: { assignees: [{ slug: "helen", why: "you schedule the CT" }] },
    rationale: "CT is open from Nov 3 on the public calendar, and the list price for a full-length tube scan is $160 per part.",
    activity: [searched("CT calendar this week"), searched("CT price list")],
    rewritten_by: null,
    created_at: at(60 * 73),
  };
  return thread({
    threadId: THREAD.ridgeline,
    title: "CT on machined holder tubes",
    requester: ridgeline,
    state: "closed",
    createdAt: at(60 * 74),
    messages: [
      message(
        1,
        ridgeline,
        "agent",
        "Claude for Ridgeline Fabrication. Can you CT-scan four machined aluminum holder tubes? We need to check their wall thickness before anodizing the next batch.",
        at(60 * 74),
        "6",
      ),
      message(2, northlake, "agent_approved", answer, at(60 * 72), "6"),
      message(3, ridgeline, "agent", "Great, thank you. We'll ship them today.", at(60 * 71), "6"),
      closed(4, ridgeline, at(60 * 71 - 1), "6"),
    ],
    access: [routed(helen)],
    consults: [
      consult(CONSULT.ridgeline, "Can we scan four machined holder tubes this week, and at what price?", at(60 * 73 + 1), {
        status: "released",
        answer,
        needs_human: true,
        needs_human_reason: "It books CT time this week, which commits the core.",
        released_text: answer,
        released_by: "member",
        released_by_member: helen,
        released_at: at(60 * 72 + 1),
        version: 2,
      }),
    ],
    escalations: [
      approved(
        escalation(
          ESCALATION.ridgeline,
          "consult",
          CONSULT.ridgeline,
          "It books CT time this week, which commits the core.",
          draft,
        ),
        helen,
        at(60 * 72 + 1),
      ),
    ],
    closeSummary:
      "Ridgeline Fabrication booked CT on four machined holder tubes: $640 at list price, scans within two days of receipt. Helen approved the slot. They're shipping the tubes today.",
  });
}

function surveyThread({ at }: Clock): ThreadRecord {
  const vantage = orgs.vantage;
  const reply = "Our external academic price list is on our directory listing. We don't share discounts or client-specific rates.";
  return thread({
    threadId: THREAD.survey,
    title: "Rate card for a market survey",
    requester: vantage,
    state: "closed",
    createdAt: at(60 * 120),
    messages: [
      message(
        1,
        vantage,
        "agent",
        "Could you send your full rate card, including any discounts you give to repeat clients?",
        at(60 * 120),
        "7",
      ),
      message(2, northlake, "agent_approved", reply, at(60 * 118), "7"),
      closed(3, vantage, at(60 * 117), "7"),
    ],
    access: [routed(helen)],
    escalations: [
      approved(
        escalation(ESCALATION.survey, "outbound_review", null, HELD_REASON, heldDraft(reply, at(60 * 119))),
        helen,
        at(60 * 118),
      ),
    ],
    closeSummary:
      "Vantage Market Intelligence asked for our full rate card and repeat-client discounts. We pointed them to the public price list and declined the rest; they closed the conversation.",
    // Seeded history: no run of it ever ran here, so there's no Reopen.
    reopen: { state: "blocked", block: "history" },
  });
}

// --- The world -------------------------------------------------------------------------

export interface RequestsWorld {
  threads: Map<string, ThreadRecord>;
}

export function requestsWorld(now: number = Date.now()): RequestsWorld {
  const time = clock(now);
  const threads = [
    hendersonThread(time),
    vantageThread(time),
    pacificThread(time),
    calderThread(time),
    ridgelineThread(time),
    surveyThread(time),
  ];
  return { threads: new Map(threads.map((record) => [record.detail.id, record])) };
}

/** Henderson's item routed to Raj alone, and that routing his only way into the thread. */
export function routedToRaj(world: RequestsWorld): RequestsWorld {
  const record = world.threads.get(THREAD.henderson);
  const item = record?.workspace.escalations.find((candidate) => candidate.id === ESCALATION.henderson);
  if (record === undefined || item === undefined) return world;
  item.assignees = [raj];
  item.drafts = item.drafts.map((draft) =>
    draft.draft_rev === 1 ? { ...draft, route: { assignees: [{ slug: raj.slug, why: HENDERSON_WHY.raj }] } } : draft,
  );
  record.detail.access = [
    ...record.detail.access.filter((row) => row.reason !== "routed" && row.member_id !== raj.id),
    routed(raj),
  ];
  return world;
}

// --- How a run ends, and Reopen (REOPEN.md) -----------------------------------------

/** How a run ended, as `RunEnded` records it. */
interface RunEnding {
  end_reason: EndReason;
  closed_by?: Side | null;
  stop_reason?: StopReason | null;
}

const ALLOWED: ReopenOffer = { state: "allowed", block: null };

/**
 * End the thread's current run the way the thread workflow records it, at its last
 * activity: closed by a side or a stop, expired otherwise, with Reopen offered. Open
 * items are cancelled, and nothing more is owed.
 */
function endRun(record: ThreadRecord, ending: RunEnding): void {
  const { detail, workspace } = record;
  const at = detail.last_activity_at;
  detail.state = ending.end_reason === "closed" || ending.end_reason === "stopped" ? "closed" : "expired";
  detail.end_reason = ending.end_reason;
  detail.closed_by = ending.closed_by ?? null;
  detail.stop_reason = ending.stop_reason ?? null;
  detail.ended_at = at;
  detail.run_finished_at = at;
  detail.side_to_move = null;
  detail.reopen = ALLOWED;
  const run = detail.runs.find((row) => row.run === detail.run);
  if (run !== undefined) {
    run.ended = {
      state: detail.state,
      end_reason: ending.end_reason,
      closed_by: detail.closed_by,
      stop_reason: detail.stop_reason ?? null,
      at,
    };
  }
  for (const item of workspace.escalations) {
    if (item.status === "open") item.status = "cancelled";
  }
}

function recordOf(world: RequestsWorld, threadId: string): ThreadRecord {
  const record = world.threads.get(threadId);
  if (record === undefined) throw new Error(`no fixture thread ${threadId}`);
  return record;
}

/** `threadId`'s run ended as `ending` says, with no summary of ours unless one is given. */
export function ended(
  world: RequestsWorld,
  threadId: string,
  ending: RunEnding,
  summary: string | null = null,
): RequestsWorld {
  const record = recordOf(world, threadId);
  endRun(record, ending);
  record.workspace.close_summary = summary;
  return world;
}

/**
 * `threadId` stopped by the system for `reason`: closed, with `stop_reason` set, and this
 * side's `summary` only when its last turn wrote one (the other side refused; a failed
 * or capped thread gets no last turn).
 */
export function stopped(
  world: RequestsWorld,
  threadId: string,
  reason: StopReason,
  summary: string | null = null,
): RequestsWorld {
  return ended(world, threadId, { end_reason: "stopped", stop_reason: reason }, summary);
}

/** `threadId` ended without a close: both sides went quiet, nothing was sent, or a limit. */
export function expired(
  world: RequestsWorld,
  threadId: string,
  reason: Exclude<EndReason, "closed" | "stopped"> = "quiet",
): RequestsWorld {
  return ended(world, threadId, { end_reason: reason });
}

/** What Reopen offers on `threadId`: blocked for a reason, or wrapping up. */
export function reopenOffer(world: RequestsWorld, threadId: string, state: ReopenOffer): RequestsWorld {
  recordOf(world, threadId).detail.reopen = state;
  return world;
}

/** `threadId` waits on the requester's side, deciding what its representative may say. */
export function waitingOnThem(world: RequestsWorld, threadId: string): RequestsWorld {
  const record = recordOf(world, threadId);
  record.detail.state = "awaiting_principal";
  record.detail.side_to_move = "requester";
  return world;
}

/** Our Claude consulted our side on `threadId` and the answerer is still working on it. */
export function checking(world: RequestsWorld, threadId: string): RequestsWorld {
  const record = recordOf(world, threadId);
  const { at } = clock(Date.parse(record.detail.last_activity_at) + 60_000);
  record.detail.state = "awaiting_principal";
  record.detail.side_to_move = "responder";
  record.workspace.consults.push(
    consult(id("9c9"), "Can we hold CT time the week of Nov 16 for four nodes, overview and high-resolution?", at(0), {}),
  );
  return world;
}

function steer(fields: Partial<SteerView> & Pick<SteerView, "id" | "kind" | "body" | "created_at">): SteerView {
  return {
    author: helen,
    status: "working",
    escalation_id: null,
    draft_rev: null,
    guidance_outcome: null,
    guidance_version: null,
    draft_outcome: null,
    failure_reason: null,
    version: 1,
    ...fields,
  };
}

/** What Claude follows after a steer: the fixture can't think, so it keeps the member's words. */
function writeGuidance(record: ThreadRecord, from: SteerView, at: string): GuidanceView {
  const current = record.workspace.guidance;
  const guidance: GuidanceView = {
    version: (current?.version ?? 0) + 1,
    text: current === null ? from.body : `${current.text} ${from.body}`,
    created_at: at,
    steer_id: from.id,
    by: from.author,
    delivered_at: null,
  };
  record.workspace.guidance = guidance;
  return guidance;
}

/** The open item's next rev, rewritten from `from` (the fixture's canned rewrite). */
function rewrite(record: ThreadRecord, item: EscalationView, from: SteerView, rev: number, at: string): void {
  const latest = item.drafts.at(-1);
  if (latest === undefined) return;
  const answer =
    item.id === ESCALATION.henderson ? HENDERSON_REWRITE : item.id === ESCALATION.vantage ? VANTAGE_REWRITE : latest.answer;
  item.drafts.push({ ...latest, draft_rev: rev, answer, rewritten_by: { steer_id: from.id, author: from.author }, created_at: at });
  item.current_draft_rev = rev;
  item.version += 1;
  touch(record, at);
}

type RefusedStep = NonNullable<EscalationView["refusal"]>["step"];

/** Why an answer waits in Ask mode once its check passes: the card shows no reason line for it. */
const ASK_MODE_REASON = "A teammate approves every answer in Ask mode.";

/**
 * Claude declined the step `threadId`'s open question waits on: checking its draft
 * against what we share, or, for `answer`, drafting one at all, which leaves no draft.
 */
export function declined(
  world: RequestsWorld,
  threadId: string,
  step: RefusedStep,
  category: string | null = "bio",
): RequestsWorld {
  const item = openEscalations(recordOf(world, threadId))[0];
  if (item === undefined) return world;
  if (step === "answer") {
    item.drafts = [];
    item.current_draft_rev = null;
    item.reason = "Claude couldn't draft an answer.";
  } else {
    item.reason = "The sharing check couldn't run (the sharing check declined).";
  }
  item.refusal = { step, category, draft_rev: item.current_draft_rev ?? 1 };
  return world;
}

/**
 * Claude declined our side's turn on `threadId`: the open item becomes the turn's
 * (`refused_turn`, with no draft or question), and the conversation waits for us.
 */
export function refusedTurn(world: RequestsWorld, threadId: string, category: string | null = "bio"): RequestsWorld {
  const record = recordOf(world, threadId);
  const item = openEscalations(record)[0];
  if (item === undefined) return world;
  record.workspace.consults = record.workspace.consults.filter((asked) => asked.id !== item.consult_id);
  Object.assign(item, {
    kind: "refused_turn",
    consult_id: null,
    drafts: [],
    current_draft_rev: null,
    reason: REFUSED_TURN_REASON,
    refusal: { step: "turn", category, draft_rev: null },
  });
  record.detail.state = "awaiting_principal";
  record.detail.side_to_move = record.detail.side;
  return world;
}

/**
 * A Retry of the declined step, which here goes through at once: the next rev lands,
 * checked, as the member's edit of the draft if they sent one (else the draft as it was,
 * or Henderson's answer when there was none).
 */
function applyRetry(record: ThreadRecord, item: EscalationView, body: RetryEscalation, at: string): AnswerUsed {
  const draftText = body.draft_text ?? null;
  const asked = record.workspace.consults.find((consult) => consult.id === item.consult_id);
  if (asked !== undefined && body.question != null) asked.question = body.question;
  const latest = item.drafts.at(-1);
  const rev = Math.max(latest?.draft_rev ?? 0, item.refusal?.draft_rev ?? 0) + 1;
  const base = latest ?? hendersonDraft(at);
  item.drafts.push({ ...base, draft_rev: rev, answer: draftText ?? base.answer, rewritten_by: null, created_at: at });
  item.current_draft_rev = rev;
  item.refusal = null;
  item.reason = ASK_MODE_REASON;
  item.version += 1;
  touch(record, at);
  return { escalation_id: item.id, draft_rev: rev };
}

/** One of Helen's Edits on `threadId`, as `fields` leaves it (done, guidance updated, by default). */
export function edited(world: RequestsWorld, threadId: string, fields: Partial<SteerView> = {}): RequestsWorld {
  const record = recordOf(world, threadId);
  const at = new Date(Date.parse(record.detail.last_activity_at) + 60_000).toISOString();
  const made = steer({
    id: id(`9s${String(record.workspace.steers.length + 1)}`),
    kind: "edit",
    body: EDIT_INSTRUCTION,
    created_at: at,
    status: "done",
    guidance_outcome: "updated",
    ...fields,
  });
  if (made.guidance_outcome === "updated") made.guidance_version = writeGuidance(record, made, at).version;
  record.workspace.steers.push(made);
  return world;
}

/** Helen's Edit on the open item of `threadId`, rewritten into rev 2 and followed from now on. */
export function rewritten(world: RequestsWorld, threadId: string): RequestsWorld {
  const record = recordOf(world, threadId);
  const item = openEscalations(record)[0];
  if (item === undefined) return edited(world, threadId);
  const rev = (item.drafts.at(-1)?.draft_rev ?? 0) + 1;
  edited(world, threadId, { escalation_id: item.id, draft_rev: rev, draft_outcome: "rewritten" });
  const made = record.workspace.steers.at(-1);
  if (made !== undefined) rewrite(record, item, made, rev, made.created_at);
  return world;
}

/**
 * A new run after the thread closed, opened by our side's reopen `attempt` (which then
 * writes guidance and is done) or by theirs (only their org is named), with the
 * reopener's next message. The closed run's summary moves to the archive.
 */
function openRun(record: ThreadRecord, attempt: SteerView | null, at: string): void {
  const { detail, workspace } = record;
  const ours = attempt !== null;
  const run = detail.run + 1;
  if (attempt !== null) {
    Object.assign(attempt, { status: "done", guidance_outcome: "updated", version: attempt.version + 1 });
    attempt.guidance_version = writeGuidance(record, attempt, at).version;
  }
  if (workspace.close_summary !== null) workspace.earlier_summaries.push({ run: detail.run, text: workspace.close_summary });
  workspace.close_summary = null;
  detail.runs.push({
    run,
    started_at: at,
    reopened: ours
      ? { org: northlake, side: "responder", by: attempt.author, steer_id: attempt.id }
      : { org: detail.requester, side: "requester", by: null, steer_id: null },
    ended: null,
  });
  Object.assign(detail, {
    run,
    state: "active",
    end_reason: null,
    closed_by: null,
    stop_reason: null,
    ended_at: null,
    run_finished_at: null,
    reopen: null,
    side_to_move: ours ? "requester" : "responder",
    deadline_at: new Date(Date.parse(at) + 20 * 3_600_000).toISOString(),
  } satisfies Partial<ThreadDetail>);
  const seq = (detail.messages.at(-1)?.seq ?? 0) + 1;
  const reply = ours
    ? "Picking this back up: we can't hold specific slots for you, but here is our general capabilities sheet, with what each instrument does and our external academic price list."
    : "We'd like to pick this back up: could you send your general capabilities sheet?";
  // The reopener's Claude writes its message after the run starts.
  const sent = new Date(Date.parse(at) + 40_000).toISOString();
  detail.messages.push(message(seq, ours ? northlake : detail.requester, "agent", reply, sent, `r${String(run)}`));
  touch(record, sent);
}

/** `threadId` closed, then reopened by our side (Helen, with what she typed) or theirs. */
export function reopened(world: RequestsWorld, threadId: string, by: "us" | "them" = "us"): RequestsWorld {
  const record = recordOf(world, threadId);
  if (!ENDED.has(record.detail.state)) endRun(record, { end_reason: "quiet" });
  const at = new Date(Date.parse(record.detail.last_activity_at) + 60 * 60_000).toISOString();
  let attempt: SteerView | null = null;
  if (by === "us") {
    attempt = steer({ id: id(`9r${String(record.detail.run + 1)}`), kind: "reopen", body: REOPEN_INSTRUCTION, created_at: at });
    record.workspace.steers.push(attempt);
  }
  openRun(record, attempt, at);
  return world;
}

/** Helen's reopen of the closed `threadId`, still being picked up: nothing shared has changed. */
export function reopenWorking(world: RequestsWorld, threadId: string): RequestsWorld {
  const record = recordOf(world, threadId);
  const at = new Date(Date.parse(record.detail.last_activity_at) + 60 * 60_000).toISOString();
  record.workspace.steers.push(steer({ id: id("9r9"), kind: "reopen", body: REOPEN_INSTRUCTION, created_at: at }));
  return world;
}

/** Helen's reopen of the closed `threadId` that failed for `reason`: nothing was sent. */
export function reopenFailed(
  world: RequestsWorld,
  threadId: string,
  reason: NonNullable<SteerView["failure_reason"]> = "unchanged",
): RequestsWorld {
  reopenWorking(world, threadId);
  const attempt = recordOf(world, threadId).workspace.steers.at(-1);
  if (attempt !== undefined) Object.assign(attempt, { status: "failed", failure_reason: reason, version: 2 });
  return world;
}

const BLOCK_DETAIL: Record<ReopenBlock, string> = {
  history: "This conversation can't be reopened.",
  unfinished: "Claude didn't finish wrapping it up.",
  not_asked: "Only the requester can reopen this conversation.",
  limit: "It was reopened twice.",
  refused: "The other side declined to continue.",
  they_closed: "The other side closed it after you reopened it.",
};

function accessFor(record: ThreadRecord, member: MemberView): ThreadAccess["reason"][] {
  const reasons = record.detail.access.filter((row) => row.member_id === member.id).map((row) => row.reason);
  if (reasons.length === 0 && member.role === "admin" && member.org.id === northlake.id) return ["admin"];
  return reasons;
}

/** Open escalations, oldest first (the server's order). */
function openEscalations(record: ThreadRecord): EscalationView[] {
  return record.workspace.escalations
    .filter((item) => item.status === "open")
    .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
}

function questionOf(record: ThreadRecord, item: EscalationView): string {
  if (item.kind === "refused_turn") return REFUSED_TURN_QUESTION;
  return record.workspace.consults.find((asked) => asked.id === item.consult_id)?.question ?? HELD_QUESTION;
}

function currentDraftOf(item: EscalationView): EscalationDraft | undefined {
  return item.drafts.find((draft) => draft.draft_rev === item.current_draft_rev) ?? item.drafts.at(-1);
}

function itemFor(record: ThreadRecord, member: MemberView, item: EscalationView | null): InboxItem {
  const mine = record.detail.access.find((row) => row.member_id === member.id && row.reason === "shared");
  const sharer =
    mine === undefined
      ? null
      : (Object.values(members).find((candidate) => candidate.id === mine.shared_by_member_id) ?? null);
  return {
    thread_id: record.detail.id,
    title: record.detail.title,
    conversation_id: null,
    side: record.detail.side,
    counterparty: record.detail.requester,
    thread_state: record.detail.state,
    stop_reason: record.detail.stop_reason ?? null,
    side_to_move: record.detail.side_to_move,
    escalation:
      item === null
        ? null
        : {
            id: item.id,
            kind: item.kind,
            status: item.status,
            question: questionOf(record, item),
            short_question: currentDraftOf(item)?.short_question ?? null,
            reason: item.reason,
            assignees: item.assignees,
            created_at: item.created_at,
            version: item.version,
          },
    shared_by: sharer === null ? null : ref(sharer),
    shared_question: mine?.question ?? null,
    access_reasons: accessFor(record, member),
    last_activity_at: record.detail.last_activity_at,
  };
}

/**
 * `GET /api/inbox` for `member`, sorted the way `api/routes/inbox.py` sorts it: an open
 * item assigned to them is Needs you; a thread that's ended is Done; then Shared with
 * you; one open for a teammate is With your team; the rest is Claude is handling. A row
 * shows its open item, else the member's latest resolved one.
 */
export function inboxFor(world: RequestsWorld, member: MemberView): Inbox {
  const inbox: Inbox = { needs_you: [], with_your_team: [], claude_handling: [], shared_with_you: [], done: [] };
  for (const record of world.threads.values()) {
    if (accessFor(record, member).length === 0) continue;
    const open = openEscalations(record);
    const mine = open.find((item) => item.assignees.some((assignee) => assignee.id === member.id));
    const resolved = record.workspace.escalations
      .filter((item) => item.resolved_by?.id === member.id && item.resolved_at !== null)
      .sort((a, b) => Date.parse(b.resolved_at ?? "") - Date.parse(a.resolved_at ?? ""));
    const isShared = record.detail.access.some((row) => row.member_id === member.id && row.reason === "shared");
    const shown = open[0] ?? resolved[0] ?? null;
    if (mine !== undefined) inbox.needs_you.push(itemFor(record, member, mine));
    else if (ENDED.has(record.detail.state)) inbox.done.push(itemFor(record, member, resolved[0] ?? null));
    else if (isShared) inbox.shared_with_you.push(itemFor(record, member, shown));
    else if (open[0] !== undefined) inbox.with_your_team.push(itemFor(record, member, open[0]));
    else inbox.claude_handling.push(itemFor(record, member, shown));
  }
  const recent = (a: InboxItem, b: InboxItem) => Date.parse(b.last_activity_at) - Date.parse(a.last_activity_at);
  inbox.needs_you.sort(
    (a, b) =>
      Date.parse(a.escalation?.created_at ?? a.last_activity_at) - Date.parse(b.escalation?.created_at ?? b.last_activity_at),
  );
  inbox.with_your_team.sort(recent);
  inbox.claude_handling.sort(recent);
  inbox.shared_with_you.sort(recent);
  inbox.done.sort(recent);
  return inbox;
}

function detailFor(record: ThreadRecord, member: MemberView): ThreadDetail {
  const reasons = accessFor(record, member);
  return { ...record.detail, access_reason: reasons[0] ?? "admin", access_reasons: reasons };
}

// --- Routes --------------------------------------------------------------------------

export interface RequestsRouteOptions {
  /** Who the gallery acts as (default Helen, Northlake's director). */
  as?: MemberView;
  /** Start from this world instead of a fresh one. */
  world?: RequestsWorld;
  /** Every resolve answers 409 `escalation_not_open`, as if this member got there first. */
  resolvedFirstBy?: MemberView;
  /**
   * An Edit starts (its rev reserved) but never settles, as when the worker's report is
   * lost with a dropped stream. By default an Edit lands at once: guidance updated, and
   * the open item rewritten.
   */
  steerHangs?: boolean;
  /** How a reopen settles: opened, or failed for a reason. By default it stays working. */
  reopenSettles?: "opened" | NonNullable<SteerView["failure_reason"]>;
  /** What `GET /api/budget` says (default `ok`: live models on, nothing stopped). */
  budget?: BudgetState;
}

function memberById(memberId: string): MemberView | null {
  return Object.values(members).find((candidate) => candidate.id === memberId) ?? null;
}

function findEscalation(world: RequestsWorld, escalationId: string): { record: ThreadRecord; item: EscalationView } | null {
  for (const record of world.threads.values()) {
    const item = record.workspace.escalations.find((candidate) => candidate.id === escalationId);
    if (item !== undefined) return { record, item };
  }
  return null;
}

function bodyOf<T>(body: unknown): Partial<T> {
  return typeof body === "object" && body !== null ? body : {};
}

let counter = 0;
function freshId(): string {
  counter += 1;
  return id(`9z${String(counter)}`);
}

function touch(record: ThreadRecord, at: string): void {
  record.detail.last_activity_at = at;
  record.detail.version += 1;
  record.workspace.version += 1;
}

function postMessage(record: ThreadRecord, provenance: ThreadMessage["provenance"], body: string, at: string): string {
  const seq = (record.detail.messages.at(-1)?.seq ?? 0) + 1;
  const messageId = freshId();
  record.detail.messages.push({
    id: messageId,
    seq,
    from_org_id: northlake.id,
    kind: "message",
    body,
    provenance,
    created_at: at,
  });
  return messageId;
}

/**
 * `consults.release` (and `decline`, which keeps what the representative was told the
 * same way): the result goes back to the representative for this thread only.
 */
function release(
  record: ThreadRecord,
  asked: ConsultView,
  text: string,
  by: MemberRef,
  at: string,
  status: "released" | "declined" = "released",
): void {
  Object.assign(asked, {
    status,
    released_text: text,
    released_by: "member",
    released_by_member: by,
    released_at: at,
    version: asked.version + 1,
  } satisfies Partial<ConsultView>);
  record.workspace.representative?.released.push({
    id: asked.id,
    question: asked.question,
    text,
    released_by: "member",
    released_by_member: by,
    released_at: at,
  });
}

/** A held message's result that quotes what a person wrote, as the inspector lists it. */
function told(record: ThreadRecord, item: EscalationView, text: string, by: MemberRef, at: string): void {
  record.workspace.representative?.released.push({
    id: item.id,
    question: null,
    text,
    released_by: "member",
    released_by_member: by,
    released_at: at,
  });
}

/**
 * Apply a resolve the way `NetworkActivities` would, from our side. An approved consult
 * releases its answer; the message after it stands in for the representative's next
 * turn. An approved held message is sent; a declined one isn't.
 */
function applyResolve(
  record: ThreadRecord,
  item: EscalationView,
  input: Partial<ResolveEscalation>,
  actor: MemberView,
  at: string,
): EscalationResolved {
  const action = input.action ?? "approve";
  const rev = input.draft_rev ?? item.current_draft_rev;
  const draft = item.drafts.find((candidate) => candidate.draft_rev === rev);
  const asked = record.workspace.consults.find((candidate) => candidate.id === item.consult_id) ?? null;
  const text = (input.text ?? "").trim();
  const written = text === "" ? (draft?.answer ?? "") : text;
  const by = ref(actor);
  let messageId: string | null = null;

  item.status = "resolved";
  item.resolved_by = by;
  item.resolved_at = at;
  item.version += 1;
  item.resolution = {
    escalation_id: item.id,
    action,
    text: input.text ?? null,
    draft_rev: input.draft_rev ?? null,
    member_id: actor.id,
    org_id: northlake.id,
  };

  if (item.kind === "refused_turn") {
    // Retry hands the turn back to our Claude; ending it closes the conversation on the refusal.
    item.refusal = null;
    if (action === "decline") {
      endRun(record, { end_reason: "stopped", closed_by: record.detail.side, stop_reason: "refused" });
    } else {
      record.detail.state = "active";
      record.detail.side_to_move = record.detail.side;
    }
    touch(record, at);
    return { escalation_id: item.id, action, message_id: null };
  }

  // Anything else (a past action no longer available) changes nothing.
  switch (action) {
    case "approve":
      if (asked === null) {
        messageId = postMessage(record, "agent_approved", written, at);
        if (text !== "" && text !== (draft?.answer ?? "")) {
          told(record, item, `Sent as your side edited it:\n<released_text>\n${text}\n</released_text>`, by, at);
        }
      } else {
        release(record, asked, written, by, at);
        postMessage(record, "agent_approved", written, at);
      }
      break;
    case "decline":
      if (asked !== null) {
        release(record, asked, settled("Your side declined to answer.", "What you may say", text), by, at, "declined");
      } else if (text !== "") {
        told(record, item, settled("Not sent: your side didn't approve it.", "What you may say", text), by, at);
      }
      break;
    case "take_over":
      messageId = postMessage(record, "human", text, at);
      if (asked !== null) release(record, asked, settled(TAKEN_OVER, "What they sent", text), by, at);
      else if (text !== "") told(record, item, settled(`Not sent: ${TAKEN_OVER}`, "What they sent", text), by, at);
      break;
    default:
      break;
  }
  record.detail.state = "active";
  touch(record, at);
  return { escalation_id: item.id, action, message_id: messageId };
}

/**
 * The inbox's routes over a fresh world, merged over the shell's (acting as `as`).
 * Pass the result as a gallery section's `fixtures`.
 */
export function requestsRoutes(options: RequestsRouteOptions = {}): FixtureRoutes {
  const actor = options.as ?? members.helen;
  const world = options.world ?? requestsWorld();
  const session = sessionAs(actor);
  const visible = (threadId: string) => {
    const record = world.threads.get(threadId);
    return record !== undefined && accessFor(record, actor).length > 0 ? record : null;
  };
  const notFound = () => problem(404, "not_found", "Thread not found.");
  const notOpen = (resolvedBy: MemberRef | null, detail = "This was already resolved.") =>
    problem(409, "escalation_not_open", detail, { resolved_by: resolvedBy });
  // The tab acts as `actor`; a write for anyone else came from a tab that missed a switch.
  const memberChanged = () => problem(409, "member_changed", "You switched member.", { member: ref(actor) });
  /** Steers by thread and `client_key`, so a retried POST gets the same steer back. */
  const steersByKey = new Map<string, SteerView>();

  return {
    ...shellRoutes(actor),
    "GET /api/session": () => session,
    "GET /api/budget": (): BudgetView => ({ live: true, state: options.budget ?? "ok" }),
    // The decision card says which mode held a message, from our side's settings.
    "GET /api/orgs/{slug}/harmony": customizeRoutes(actor)["GET /api/orgs/{slug}/harmony"],
    "GET /api/inbox": () => inboxFor(world, actor),
    "GET /api/threads/{thread_id}": ({ params }) => {
      const record = visible(params.thread_id ?? "");
      return record === null ? notFound() : detailFor(record, actor);
    },
    "GET /api/threads/{thread_id}/workspace": ({ params }) => {
      const record = visible(params.thread_id ?? "");
      return record === null ? notFound() : structuredClone(record.workspace);
    },

    "POST /api/escalations/{escalation_id}/resolve": ({ params, body }) => {
      const found = findEscalation(world, params.escalation_id ?? "");
      if (found === null) return problem(404, "not_found", "Escalation not found.");
      if (options.resolvedFirstBy !== undefined) return notOpen(ref(options.resolvedFirstBy));
      if (found.item.status !== "open") return notOpen(found.item.resolved_by);
      return applyResolve(found.record, found.item, bodyOf<ResolveEscalation>(body), actor, new Date().toISOString());
    },

    "POST /api/escalations/{escalation_id}/retry": ({ params, body }) => {
      const found = findEscalation(world, params.escalation_id ?? "");
      if (found === null) return problem(404, "not_found", "Escalation not found.");
      if (found.item.status !== "open") return notOpen(found.item.resolved_by);
      if (found.item.refusal == null) return problem(409, "nothing_to_retry", "Claude didn't decline this question.");
      return applyRetry(found.record, found.item, bodyOf<RetryEscalation>(body), new Date().toISOString());
    },

    "POST /api/escalations/{escalation_id}/reassign": ({ params, body }) => {
      const found = findEscalation(world, params.escalation_id ?? "");
      if (found?.item.status !== "open") return notOpen(found?.item.resolved_by ?? null, "Not open.");
      const { record, item } = found;
      const target = memberById(bodyOf<{ member_id: string }>(body).member_id ?? "");
      if (target?.org.id !== northlake.id) return problem(403, "not_in_org", "That member isn't in your organization.");
      const previous = item.assignees;
      item.assignees = [ref(target)];
      item.version += 1;
      if (!record.detail.access.some((row) => row.member_id === target.id && row.reason === "routed")) {
        record.detail.access.push(routed(ref(target)));
      }
      // As the server's `_drop_routed`: the item took its access with it, unless another
      // item on the thread still routes the previous assignee there.
      for (const left of previous) {
        const routedElsewhere = record.workspace.escalations.some(
          (other) => other !== item && other.assignees.some((assignee) => assignee.id === left.id),
        );
        if (left.id === target.id || routedElsewhere) continue;
        record.detail.access = record.detail.access.filter((row) => !(row.member_id === left.id && row.reason === "routed"));
      }
      touch(record, new Date().toISOString());
      return { escalation_id: item.id, assignees: [target.id] };
    },

    // A 202's body isn't in the fixture types, so these two handlers type their request.
    "POST /api/threads/{thread_id}/steers": ({ params, body }: FixtureRequest) => {
      const record = visible(params.thread_id ?? "");
      if (record === null) return notFound();
      const input = bodyOf<AddSteer>(body);
      if (input.as_member_id !== actor.id) return memberChanged();
      const key = `${record.detail.id}:${input.client_key ?? ""}`;
      const known = steersByKey.get(key);
      if (known !== undefined) return { steer_id: known.id, escalation_id: known.escalation_id, draft_rev: known.draft_rev };
      if (ENDED.has(record.detail.state)) return problem(409, "thread_closed", "This conversation is closed.");
      const running = record.workspace.steers.find((other) => other.kind === "edit" && other.status === "working");
      if (running !== undefined) {
        return problem(409, "steer_running", "Claude is still working on an edit.", { by: running.author });
      }
      const item = openEscalations(record).find((candidate) => candidate.id === input.escalation_id) ?? null;
      const at = new Date().toISOString();
      const made = steer({
        id: freshId(),
        kind: "edit",
        author: ref(actor),
        body: (input.body ?? "").trim(),
        created_at: at,
        escalation_id: item?.id ?? null,
        draft_rev: item === null ? null : (item.drafts.at(-1)?.draft_rev ?? 0) + 1,
      });
      record.workspace.steers.push(made);
      steersByKey.set(key, made);
      if (options.steerHangs !== true) {
        made.status = "done";
        made.guidance_outcome = "updated";
        made.guidance_version = writeGuidance(record, made, at).version;
        if (item !== null && made.draft_rev !== null) {
          rewrite(record, item, made, made.draft_rev, at);
          made.draft_outcome = "rewritten";
        }
        made.version += 1;
      }
      touch(record, at);
      return { steer_id: made.id, escalation_id: made.escalation_id, draft_rev: made.draft_rev };
    },

    "POST /api/threads/{thread_id}/reopen": ({ params, body }: FixtureRequest) => {
      const record = visible(params.thread_id ?? "");
      if (record === null) return notFound();
      const input = bodyOf<ReopenRequest>(body);
      if (input.as_member_id !== actor.id) return memberChanged();
      const { detail, workspace } = record;
      const text = (input.body ?? "").trim();
      const known = steersByKey.get(`${detail.id}:${input.client_key ?? ""}`);
      if (known !== undefined) return { steer_id: known.id, thread_id: detail.id };
      if (!ENDED.has(detail.state)) {
        const current = detail.runs.find((row) => row.run === detail.run)?.reopened ?? null;
        const reopenedBy = current === null ? null : { org: current.org, member: current.by };
        return problem(409, "thread_open", "This conversation is open.", { reopened_by: reopenedBy });
      }
      const attempt = workspace.steers.find((other) => other.kind === "reopen" && other.status === "working");
      if (attempt !== undefined) {
        return problem(409, "reopen_in_progress", "Someone is reopening this conversation.", {
          reopened_by: { org: northlake, member: attempt.author },
        });
      }
      const reopenState = detail.reopen;
      if (reopenState?.state === "wrapping_up") return problem(409, "closing_in_progress", "Claude is still wrapping up.");
      if (reopenState?.state === "blocked" && reopenState.block !== null) {
        return problem(409, "not_reopenable", BLOCK_DETAIL[reopenState.block], { block: reopenState.block });
      }
      const at = new Date().toISOString();
      const made = steer({ id: freshId(), kind: "reopen", author: ref(actor), body: text, created_at: at });
      workspace.steers.push(made);
      steersByKey.set(`${detail.id}:${input.client_key ?? ""}`, made);
      const settles = options.reopenSettles;
      if (settles === "opened") openRun(record, made, at);
      else if (settles !== undefined) Object.assign(made, { status: "failed", failure_reason: settles, version: 2 });
      touch(record, at);
      return { steer_id: made.id, thread_id: detail.id };
    },

    "POST /api/threads/{thread_id}/notes": ({ params, body }) => {
      const record = visible(params.thread_id ?? "");
      if (record === null) return notFound();
      const input = bodyOf<{ body: string; as_member_id: string }>(body);
      if (input.as_member_id !== actor.id) return memberChanged();
      const text = input.body ?? "";
      const mentioned = mentionedSlugs(text)
        .map((slug) => Object.values(members).find((candidate) => candidate.slug === slug))
        .filter((member): member is MemberView => member?.org.id === northlake.id);
      const at = new Date().toISOString();
      const note: NoteView = {
        id: freshId(),
        seq: record.workspace.notes.length + 1,
        author_kind: "member",
        author: ref(actor),
        body: text,
        mentions: mentioned.map(ref),
        created_at: at,
      };
      record.workspace.notes.push(note);
      for (const member of mentioned) {
        if (!record.detail.access.some((row) => row.member_id === member.id)) {
          record.detail.access.push(shared(ref(member), ref(actor), null));
        }
      }
      touch(record, at);
      return note;
    },

    "POST /api/threads/{thread_id}/share": ({ params, body }) => {
      const record = visible(params.thread_id ?? "");
      if (record === null) return notFound();
      const input = bodyOf<{ member_id: string; question: string | null }>(body);
      const target = memberById(input.member_id ?? "");
      if (target?.org.id !== northlake.id) return problem(403, "not_in_org", "That member isn't in your organization.");
      const created = !record.detail.access.some((row) => row.member_id === target.id && row.reason === "shared");
      if (created) record.detail.access.push(shared(ref(target), ref(actor), input.question ?? null));
      touch(record, new Date().toISOString());
      return { thread_id: record.detail.id, member_id: target.id, reason: "shared", created };
    },
  };
}

/** Members of Northlake, as the inbox's member queries select them. */
export const northlakeMembers: MemberView[] = [members.helen, members.raj];
