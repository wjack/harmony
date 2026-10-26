/**
 * The Requests inbox's fixtures: Northlake's side of the harmony-demo story
 * (docs/REVISION_UNSTRUCTURED.md §10). Helen's inbox holds Henderson Lab's XRD request,
 * where Claude's draft uses Raj's internal reserve block and the sharing check stops
 * it, and a reply to Vantage held for review because Northlake trusts Vantage on Ask.
 * Beside them sit threads Claude is handling and ones already closed.
 *
 * The routes are a small in-memory server: resolving, reassigning, redrafting, notes
 * and sharing change the world, so the gallery's buttons really move items between
 * sections. Timestamps are relative to `now`, so ages read as they would in the app.
 */
import { northlakeSettings } from "./customize";
import { problem, type FixtureRoutes } from "./index";
import { members, orgs, sessionAs, shellRoutes } from "./shell";
import type {
  ActivityLine,
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

const READ_DOCUMENT: ActivityLine = { tool: "read_document", label: "Read a document", detail: null };

// --- Drafts ------------------------------------------------------------------------------

export const HENDERSON_QUESTION =
  "Can we run XRD on 12 air-sensitive powders before Nov 6? The public calendar is full until Nov 10.";

const HENDERSON_ANSWER =
  "Yes, we can fit them in. There's XRD time on the morning of Wednesday, Nov 4, and SEM/EDS is open that week, so results would be ready by Thursday, Nov 5.\n\n" +
  "At our external academic list price that's $1,410: 12 × ($45 XRD + $60 SEM/EDS), plus $150 for air-free handling. " +
  "The powders need to arrive sealed under argon by Tuesday, Nov 3; we load them in our glovebox into airtight dome holders.";

const HENDERSON_SHARING_REASON =
  "The answer mentions an internal reserve XRD slot (Wed, Nov 4). Northlake shares what's open on the public calendar, not internal reserve slots.";

const HENDERSON_ROUTE = {
  assignees: ["helen"],
  reason: "Instrument priority and reserve-slot requests go to Helen.",
};

/** Claude's first draft for Henderson (the escalation's rev 1). */
export function hendersonDraft(createdAt: string): EscalationDraft {
  return {
    draft_rev: 1,
    answer: HENDERSON_ANSWER,
    route: HENDERSON_ROUTE,
    rationale:
      "The public calendar has XRD booked until Nov 10, so without Raj's reserve block the honest answer is no. " +
      "His note puts the block on Wed, Nov 4, 09:00 to 13:00, enough for about 14 powders. " +
      "The price is the external academic list: 12 × ($45 + $60) + $150 = $1,410.",
    activity: [
      searched("XRD availability before Nov 6"),
      searched("reserve XRD block"),
      READ_DOCUMENT,
      searched("external academic price list XRD SEM air-free handling"),
    ],
    created_at: createdAt,
  };
}

/** What "Redraft" returns for Henderson (the fixture can't think). */
function hendersonRedraft(rev: number, createdAt: string): EscalationDraft {
  const first = hendersonDraft(createdAt);
  return {
    ...first,
    draft_rev: rev,
    answer:
      "Yes. We can run XRD on Wednesday, Nov 4 and SEM/EDS the same week, with results to you by Thursday, Nov 5. " +
      "At list price that's $1,410 for all 12.\n\n" +
      "Please ship the powders sealed under argon to arrive by Tuesday, Nov 3; we'll handle the glovebox transfer on our side.",
    rationale: `Redrafted as asked: shorter, and the shipping deadline is its own paragraph. ${first.rationale}`,
  };
}

/** A message the representative wrote in Ask mode, held as an `outbound_review` draft. */
function heldDraft(answer: string, createdAt: string): EscalationDraft {
  return { draft_rev: 1, answer, route: null, rationale: HELD_REASON, activity: [], created_at: createdAt };
}

const VANTAGE_REPLY =
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
  return { parts, notices: [], released: [], frozen_at: frozenAt };
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
}

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
  const last = [parts.messages.at(-1)?.created_at, notes.at(-1)?.created_at, parts.createdAt]
    .filter((at): at is string => at !== undefined)
    .sort((a, b) => Date.parse(b) - Date.parse(a))[0];
  return {
    detail: {
      id: parts.threadId,
      title: parts.title,
      state: parts.state,
      side: "responder",
      requester: parts.requester,
      responder: northlake,
      last_activity_at: last ?? parts.createdAt,
      version: 1,
      side_to_move: null,
      deadline_at: parts.deadline ?? null,
      created_at: parts.createdAt,
      messages: parts.messages,
      access: parts.access,
    },
    workspace: {
      thread_id: parts.threadId,
      side: "responder",
      consults,
      escalations: parts.escalations ?? [],
      notes,
      representative: context,
      close_summary: parts.closeSummary ?? null,
      version: 1,
    },
  };
}

function hendersonThread({ at }: Clock): ThreadRecord {
  const henderson = orgs.henderson;
  return thread({
    threadId: THREAD.henderson,
    title: "XRD and SEM/EDS for 12 air-sensitive powders",
    requester: henderson,
    state: "awaiting_principal",
    createdAt: at(12),
    deadline: "2026-11-06T17:00:00Z",
    messages: [
      message(
        1,
        henderson,
        "agent",
        "Hello from Claude for Henderson Lab, writing for Alex Chen in Prof. Maya Henderson's group.\n\n" +
          "We need powder XRD and SEM/EDS on 12 air-sensitive sodium-ion cathode powders (layered oxides), with results by Friday, Nov 6. " +
          "The samples have to stay under argon; we can ship them sealed.\n\n" +
          "Your public calendar shows XRD booked until Nov 10. Is there any way to fit them in before Nov 6, and what would it cost?",
        at(12),
        "1",
      ),
    ],
    access: [routed(helen), shared(raj, helen, "Can you run the reserve block on Wednesday?")],
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
        body: "@raj can you run the reserve block on Wednesday morning? It'd be 12 powders.",
        mentions: [raj],
        created_at: at(7),
      },
      {
        id: id("9n12"),
        seq: 2,
        author_kind: "member",
        author: raj,
        body: "Yes, 9 to 1 works if the powders are in the glovebox by Tuesday evening. I'll hold it until noon tomorrow.",
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
    title: "Thursday SEM availability",
    requester: vantage,
    state: "awaiting_principal",
    createdAt: at(46),
    messages: [
      message(
        1,
        vantage,
        "agent",
        "Hi, this is Claude for Vantage Market Intelligence. We're mapping characterization capacity in the region. " +
          "Which clients currently hold your Thursday SEM slots, and roughly what do they pay per hour?",
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
  const answer = "About a week from receipt; $120 per sample at list price.";
  return thread({
    threadId: THREAD.pacific,
    title: "SEM cross-sections of coated foils",
    requester: pacific,
    state: "active",
    createdAt: at(95),
    messages: [
      message(
        1,
        pacific,
        "agent",
        "Claude for Pacific Beamline. We'd like SEM cross-sections of four coated copper foils before our December beamtime. What's your lead time for FIB-free cross-sections?",
        at(95),
        "4",
      ),
      message(
        2,
        northlake,
        "agent",
        "Broad-ion-beam cross-sections run about a week from receipt. Four foils would be $480 at list price ($120 each). Do you want them imaged at 5 kV and 15 kV?",
        at(93),
        "4",
      ),
      message(3, pacific, "agent", "Both, please. Can you hold time the week of Nov 16?", at(18), "4"),
    ],
    access: [routed(helen)],
    consults: [
      consult(CONSULT.pacific, "What's our lead time and price for broad-ion-beam SEM cross-sections?", at(94), {
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
    title: "Reference XRD patterns for DFT validation",
    requester: calder,
    state: "active",
    createdAt: at(60 * 5),
    messages: [
      message(
        1,
        calder,
        "agent",
        "Claude for Calder Computational. We need high-resolution XRD on eight reference oxides to validate DFT lattice parameters, ideally in December. Is that something you take on?",
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
  const answer = "Yes: we can image all four this week. $640 at list price ($160 each), images within two days of receipt.";
  const draft: EscalationDraft = {
    draft_rev: 1,
    answer,
    route: { assignees: ["helen"], reason: "Instrument scheduling goes to Helen." },
    rationale: "SEM is open from Nov 3 on the public calendar, and the list price for secondary-electron imaging is $160 per sample.",
    activity: [searched("SEM calendar this week"), searched("SEM price list")],
    created_at: at(60 * 73),
  };
  return thread({
    threadId: THREAD.ridgeline,
    title: "SEM on machined PEEK holder surfaces",
    requester: ridgeline,
    state: "closed",
    createdAt: at(60 * 74),
    messages: [
      message(
        1,
        ridgeline,
        "agent",
        "Claude for Ridgeline Fabrication. Can you image the machined sealing faces of four PEEK sample holders? We need to check tool marks before anodizing the next batch.",
        at(60 * 74),
        "6",
      ),
      message(2, northlake, "agent_approved", answer, at(60 * 72), "6"),
      message(3, ridgeline, "agent", "Great, thank you. We'll ship them today.", at(60 * 71), "6"),
      closed(4, ridgeline, at(60 * 71 - 1), "6"),
    ],
    access: [routed(helen)],
    consults: [
      consult(CONSULT.ridgeline, "Can we image four machined PEEK holders this week, and at what price?", at(60 * 73 + 1), {
        status: "released",
        answer,
        needs_human: true,
        needs_human_reason: "It books SEM time this week, which commits the core.",
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
          "It books SEM time this week, which commits the core.",
          draft,
        ),
        helen,
        at(60 * 72 + 1),
      ),
    ],
    closeSummary:
      "Ridgeline Fabrication booked SEM on four machined PEEK holders: $640 at list price, images within two days of receipt. Helen approved the slot. They're shipping the holders today.",
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

const ENDED: ReadonlySet<ThreadState> = new Set(["closed", "expired"]);

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
  return record.workspace.consults.find((asked) => asked.id === item.consult_id)?.question ?? HELD_QUESTION;
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
    side_to_move: record.detail.side_to_move,
    escalation:
      item === null
        ? null
        : {
            id: item.id,
            kind: item.kind,
            status: item.status,
            question: questionOf(record, item),
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
   * A redraft starts but never lands, as when the worker's failure report is lost with a
   * dropped stream; asking again meanwhile gets the server's 409 with no `resolved_by`.
   */
  redraftNeverLands?: boolean;
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
  const redrafting = new Set<string>();

  return {
    ...shellRoutes(actor),
    "GET /api/session": () => session,
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

    "POST /api/escalations/{escalation_id}/reassign": ({ params, body }) => {
      const found = findEscalation(world, params.escalation_id ?? "");
      if (found?.item.status !== "open") return notOpen(found?.item.resolved_by ?? null, "Not open.");
      const { record, item } = found;
      const target = memberById(bodyOf<{ member_id: string }>(body).member_id ?? "");
      if (target?.org.id !== northlake.id) return problem(403, "not_in_org", "That member isn't in your organization.");
      item.assignees = [ref(target)];
      item.version += 1;
      if (!record.detail.access.some((row) => row.member_id === target.id && row.reason === "routed")) {
        record.detail.access.push(routed(ref(target)));
      }
      touch(record, new Date().toISOString());
      return { escalation_id: item.id, assignees: [target.id] };
    },

    "POST /api/escalations/{escalation_id}/redraft": ({ params }) => {
      const found = findEscalation(world, params.escalation_id ?? "");
      // Like the workflow's validator: only an open consult, one redraft at a time.
      if (found?.item.status !== "open" || found.item.kind !== "consult" || redrafting.has(found.item.id)) {
        return notOpen(null, `escalation ${params.escalation_id ?? ""} can't be redrafted now`);
      }
      const { record, item } = found;
      const latest = item.drafts.at(-1);
      const rev = (latest?.draft_rev ?? 0) + 1;
      if (options.redraftNeverLands === true) {
        redrafting.add(item.id);
        return { escalation_id: item.id, draft_rev: rev };
      }
      const at = new Date().toISOString();
      const next =
        item.id === ESCALATION.henderson
          ? hendersonRedraft(rev, at)
          : latest === undefined
            ? undefined
            : { ...latest, draft_rev: rev, created_at: at };
      if (next !== undefined) {
        item.drafts.push(next);
        item.current_draft_rev = rev;
        item.version += 1;
        touch(record, at);
      }
      return { escalation_id: item.id, draft_rev: rev };
    },

    "POST /api/threads/{thread_id}/notes": ({ params, body }) => {
      const record = visible(params.thread_id ?? "");
      if (record === null) return notFound();
      const text = bodyOf<{ body: string }>(body).body ?? "";
      const mentioned = [...text.matchAll(/@([a-z0-9_-]+)/gi)]
        .map((match) => Object.values(members).find((candidate) => candidate.slug === match[1]?.toLowerCase()))
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
