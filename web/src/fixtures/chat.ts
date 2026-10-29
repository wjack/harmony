/**
 * The chat's fixtures: Alex's conversation from the harmony-demo story
 * (REVISION_UNSTRUCTURED.md §10), at each stage from the first search to both threads
 * closed, plus the request status and the routes that move it along. The routes are
 * stateful, so approving the card or answering a question in the gallery really
 * advances the chat. `ChatOptions` gives the variants the live demo adds: a chat that
 * sends without review, a failed turn, a stopped thread, and a card turned down or
 * redrafted.
 */
import type { EventEnvelope } from "../api/types";
import type { FailureReason } from "../chat/failureCopy";
import type {
  ApproveCard,
  AssistantTextItem,
  CardRecipient,
  CardRejectedWith,
  ChatItem,
  ConversationDetail,
  InlineQuestionItem,
  MemberRef,
  OutreachCardItem,
  RecipientStatus,
  RequestStatusView,
  ScreenedCounts,
  SendMode,
  ThinkingSummaryItem,
  ToolActivityItem,
  UserTextItem,
} from "../chat/types";
import { problem, type FixtureRoutes } from "./index";
import { conversations, FIXTURE_NOW, members, orgs, story, WORLD_ID } from "./shell";

/** Where the story is: each stage adds to the one before. */
export type ChatStage = "working" | "drafted" | "sent" | "question" | "go_ahead" | "closed";

const STAGES: readonly ChatStage[] = ["working", "drafted", "sent", "question", "go_ahead", "closed"];

function reached(stage: ChatStage, target: ChatStage): boolean {
  return STAGES.indexOf(stage) >= STAGES.indexOf(target);
}

function id(suffix: string): string {
  return `0b1d7a3e-0000-4000-8000-${suffix.padStart(12, "0")}`;
}

export const CONVERSATION_ID = conversations[0]?.id ?? id("2a1");
export const OUTREACH_CARD_ID = id("5c1");
/** The draft Claude writes after a message turns the first one down. */
export const REDRAFT_CARD_ID = id("5c2");
export const REQUEST_ID = id("6a1");
export const NORTHLAKE_THREAD_ID = id("6b1");
export const RIDGELINE_THREAD_ID = id("6b2");
export const TUBE_QUESTION_ID = id("6e2");
export const NORTHLAKE_GO_AHEAD_ID = id("6e3");
export const RIDGELINE_GO_AHEAD_ID = id("6e4");

const MESSAGE_ID = id("7a1");
/** Alex sends the walkthrough's suggested message as it stands. */
export const OPENING_MESSAGE = story.suggested_message ?? "";

export const TURN = {
  search: id("8d1"),
  send: id("8d2"),
  summary: id("8d3"),
  redraft: id("8d4"),
  declined: id("8d5"),
  refused: id("8d6"),
} as const;

function minutesAgo(minutes: number): string {
  return new Date(Date.parse(FIXTURE_NOW) - minutes * 60_000).toISOString();
}

function ref(member: { id: string; slug: string; name: string }): MemberRef {
  return { id: member.id, slug: member.slug, name: member.name };
}

// --- The brief and the recipients ----------------------------------------------------

export const brief = [
  "CT scans and compression tests on 12 titanium lattice coupons, plus a CT holder",
  "",
  "Henderson Frame Lab needs CT scans and compression tests on 12 lattice coupons, 20 mm cubes printed in Ti-6Al-4V titanium, with results by Nov 6. A compression test crushes the coupon, so each one is scanned first.",
  "",
  "We also need a holder made that stacks the 12 coupons for one CT session, delivered to the core before Nov 3.",
  "",
  "Please reply with what you can do, the price and the dates. Alex can drop the coupons off at the core by Nov 3.",
].join("\n");

/** The request's name for Alex, as `story_suggested_chat.yaml` has Claude write it. */
export const REQUEST_TITLE = "Lattice coupon testing and holder";

/**
 * Who Claude lists, with the subject each one reads and what only our representative
 * in that conversation is asked (LIVE_DEMO_PLAN 3.13 E2, E4, E7). Calder is listed for
 * Alex's decision, so the card starts it unticked.
 */
export const cardRecipients: CardRecipient[] = [
  {
    org: orgs.northlake,
    fit: "strong",
    reason: "Micro-CT and compression testing on campus, and a CT session takes up to 16 coupons.",
    title: "CT and compression quote",
    ask: "Quote CT scans and compression tests on all 12 coupons, CT first, with results by Nov 6. Up to about $1,600; check with Alex before committing more than $1,000.",
    suggested: true,
    selected: true,
  },
  {
    org: orgs.ridgeline,
    fit: "strong",
    reason: "Machines stacked CT holders and test fixtures in 3–5 business days.",
    title: "CT holder quote",
    ask: "Quote one stacked CT holder for the 12 coupons, with the tube options and a delivery date before Nov 3. Keep it under $400.",
    suggested: true,
    selected: true,
  },
  {
    org: orgs.calder,
    fit: "no",
    reason: "Computation only: finite-element studies, no lab instruments.",
    title: "Modelling support enquiry",
    ask: "Ask whether they could model how these lattices crush later in the project. Nothing to commit to now.",
    suggested: false,
    selected: true,
  },
];

/**
 * Claude's redraft after Alex's message (`REDRAFT_MESSAGE`): the brief says how the
 * coupons are marked, and both asks change; Calder's stays as it was.
 */
export const REDRAFT_MESSAGE = "Say each coupon is engraved with its ID, and ask Ridgeline which tubes they supply.";

export const redraftBrief = brief.replace(
  "so each one is scanned first.",
  "so each one is scanned first. Each coupon is engraved with its ID, so results can be reported by ID.",
);

const REDRAFT_ASKS: Readonly<Record<string, string>> = {
  [orgs.northlake.id]:
    "Quote CT scans and compression tests on all 12 coupons, each engraved with its ID, CT first, with results by Nov 6. Up to about $1,600; check with Alex before committing more than $1,000.",
  [orgs.ridgeline.id]:
    "Quote one stacked CT holder for the 12 coupons: which tubes they supply, what each costs and the delivery date. Keep it under $400.",
};

export const REDRAFT_REPLY =
  "I've said each coupon is engraved with its ID, and asked Ridgeline which tubes they supply. Here's the new draft for your OK.";

/** Claude's one line after a Decline with a reason; with none it says nothing. */
export const DECLINED_REPLY = "Understood, I won't send it. Tell me if you'd like a different request.";

/** Claude's one sentence once the request is out: where its progress shows. */
export const SENT_REPLY = "It's out to Northlake and Ridgeline; their progress shows on the request's card.";

export const screened: ScreenedCounts = { total: 14, strong: 2, possible: 4, no: 8, unknown: 0 };

/** What Alex answers, unless a gallery visitor types something else. */
export const ANSWERS = {
  tube: "Carbon fiber, please. We need high-resolution scans to see strut defects.",
  northlake: "Yes, take priority.",
  ridgeline: "Yes, go ahead with Ridgeline, with the carbon-fiber tube.",
} as const;

// --- Items ---------------------------------------------------------------------------

function user(messageId: string, text: string, minutes: number): UserTextItem {
  return { id: `msg:${messageId}:0`, at: minutesAgo(minutes), type: "user_text", text, member_id: members.alex.id };
}

function reply(turnId: string, block: number, markdown: string, minutes: number): AssistantTextItem {
  return { id: `turn:${turnId}:${block}`, at: minutesAgo(minutes), type: "assistant_text", markdown, turn_id: turnId };
}

function thinking(turnId: string, text: string, minutes: number): ThinkingSummaryItem {
  return { id: `think:${turnId}`, at: minutesAgo(minutes), type: "thinking_summary", text, turn_id: turnId };
}

function tool(
  turnId: string,
  name: string,
  label: string,
  status: ToolActivityItem["status"],
  minutes: number,
  { detail = null, outcome = null, n = 0 }: { detail?: string | null; outcome?: string | null; n?: number } = {},
): ToolActivityItem {
  return {
    id: `tool:${turnId}:${name}:${String(n)}`,
    at: minutesAgo(minutes),
    type: "tool_activity",
    tool: name,
    label,
    detail,
    status,
    outcome,
    turn_id: turnId,
  };
}

/** The two needs Claude searches the network for, as it words them, and what each found. */
export const SEARCHES = [
  {
    need: "CT scans and compression tests on 12 titanium lattice coupons, CT first, results by Nov 6",
    outcome: "1 strong, 2 possible of 14",
  },
  { need: "A holder that stacks the 12 coupons for one CT session", outcome: "1 strong, 1 possible of 14" },
] as const;

/** Running, the line reads as BE4 builds it: present tense, nothing found yet. */
export const RUNNING_SEARCH_LABEL = "Searching the Harmony network…";

export const FIRST_REPLY = [
  "I screened the network and found two organizations that fit, plus one near miss:",
  "",
  "- **Northlake Characterization Core** runs micro-CT and compression testing, and scans up to 16 coupons in one session.",
  "- **Ridgeline Fabrication** machines stacked CT holders and test fixtures in 3–5 business days.",
  "- **Calder Computational** only does computation, so I've left it unticked.",
  "",
  "The brief tells them the coupon count, the Nov 6 date and that each coupon is scanned before it's crushed, and leaves out the lattice designs. Each organization's ask carries its share of your budget, which only your representative sees. Here it is for your OK.",
].join("\n");

export const CLOSING_SUMMARY = [
  "Both are booked:",
  "",
  "- **Northlake** scans all 12 coupons on Wed Nov 4 and tests them in its priority compression block that afternoon for **$1,434**, with all reports by Nov 6. Drop the coupons at the core's front desk by Nov 3.",
  "- **Ridgeline** makes the stacked CT holder with a carbon-fiber tube for **$325**, delivered to the core on Nov 2.",
  "",
  "That's **$1,759** in total, inside your $2,000 budget.",
].join("\n");

/** How a card was turned down: its Decline, with an optional reason, or a message typed while it waited. */
export interface Rejection {
  with: CardRejectedWith;
  text: string | null;
}

/** An approved card reads as it went out: the member's ticks and wording. */
function asApproved(recipients: readonly CardRecipient[], cardBrief: string, body: ApproveCard | null) {
  const chosen = body?.recipients ?? recipients.filter((r) => r.suggested === true).map((r) => r.org.id);
  return {
    brief: body?.brief ?? cardBrief,
    recipients: recipients.map((r) => ({ ...r, ask: body?.asks?.[r.org.id] ?? r.ask, selected: chosen.includes(r.org.id) })),
  };
}

function outreachCard(state: ChatState, draft: "first" | "redraft"): OutreachCardItem {
  const { options } = state;
  const auto = options.auto ?? false;
  const rejection = draft === "first" ? state.first : state.second;
  const failed = draft === "first" && options.autoFailure !== undefined;
  const sent = reached(state.stage, "sent") && rejection === null && !failed;
  const rejected = rejection !== null || failed;
  const cardId = draft === "first" ? OUTREACH_CARD_ID : REDRAFT_CARD_ID;
  const recipients =
    draft === "first" ? cardRecipients : cardRecipients.map((r) => ({ ...r, ask: REDRAFT_ASKS[r.org.id] ?? r.ask }));
  const cardBrief = draft === "first" ? brief : redraftBrief;
  // Like `domain/conversations.py`: once approved, and while an unreviewed send goes
  // out, those left out aren't selected; pending or rejected, every row is.
  const decided = sent || (auto && !rejected) ? asApproved(recipients, cardBrief, state.approved) : null;
  return {
    id: `card:${cardId}`,
    at: minutesAgo(draft === "first" ? 40 : 39),
    type: "outreach_card",
    card_id: cardId,
    status: sent ? "approved" : rejected ? "rejected" : "pending",
    rejected_with: rejection?.with ?? null,
    title: REQUEST_TITLE,
    auto,
    brief: decided?.brief ?? cardBrief,
    recipients: decided?.recipients ?? recipients,
    screened,
    feedback: rejection?.text ?? (failed ? (options.autoFailure ?? null) : null),
    request_id: sent ? REQUEST_ID : null,
    version: sent || rejected ? 2 : 1,
  };
}

/**
 * The drafts and what followed each one being turned down: a message brings Claude's
 * redraft; a Decline with a reason, Claude's one line. Also which card waits, if any.
 */
function drafts(state: ChatState): { items: ChatItem[]; pending: string | null } {
  const items: ChatItem[] = [outreachCard(state, "first")];
  const { first, second } = state;
  if (first === null) return { items, pending: OUTREACH_CARD_ID };
  if (first.with === "decline") {
    if (first.text !== null) items.push(reply(TURN.declined, 0, DECLINED_REPLY, 39));
    return { items, pending: null };
  }
  items.push(reply(TURN.redraft, 0, REDRAFT_REPLY, 39), outreachCard(state, "redraft"));
  if (second?.with === "decline" && second.text !== null) items.push(reply(TURN.declined, 0, DECLINED_REPLY, 38));
  return { items, pending: second === null ? REDRAFT_CARD_ID : null };
}

interface QuestionSpec {
  escalationId: string;
  threadId: string;
  from: InlineQuestionItem["from_org"];
  question: string;
  /** Shown from this stage on. */
  askedAt: ChatStage;
  /** Answered with `answer` from this stage on, unless the gallery answered first. */
  answeredBy: ChatStage;
  answer: string;
  minutes: number;
}

const TUBE: QuestionSpec = {
  escalationId: TUBE_QUESTION_ID,
  threadId: RIDGELINE_THREAD_ID,
  from: orgs.ridgeline,
  question:
    "Ridgeline asks which tube the holder should use: aluminum, which is included, or carbon fiber for $85 more, which they recommend for high-resolution scans. Which would you like?",
  askedAt: "question",
  answeredBy: "go_ahead",
  answer: ANSWERS.tube,
  minutes: 18,
};

const NORTHLAKE_GO_AHEAD: QuestionSpec = {
  escalationId: NORTHLAKE_GO_AHEAD_ID,
  threadId: NORTHLAKE_THREAD_ID,
  from: orgs.northlake,
  question:
    "Northlake quotes two options for CT and compression tests on all 12 coupons: standard, $1,284 with reports by Nov 13, or priority, $1,434 with compression reports by Nov 5 and CT by Nov 6. Both are over the $1,000 I may commit alone. Which should I book?",
  askedAt: "go_ahead",
  answeredBy: "closed",
  answer: ANSWERS.northlake,
  minutes: 7,
};

const RIDGELINE_GO_AHEAD: QuestionSpec = {
  escalationId: RIDGELINE_GO_AHEAD_ID,
  threadId: RIDGELINE_THREAD_ID,
  from: orgs.ridgeline,
  question: "Ridgeline quotes $325 for the stacked CT holder with a carbon-fiber tube, delivered Nov 2. Go ahead?",
  askedAt: "go_ahead",
  answeredBy: "closed",
  answer: ANSWERS.ridgeline,
  minutes: 6,
};

const QUESTIONS: readonly QuestionSpec[] = [TUBE, NORTHLAKE_GO_AHEAD, RIDGELINE_GO_AHEAD];

/** The member's answer to a question, or null while it's open. */
function answerTo(state: ChatState, spec: QuestionSpec): string | null {
  return state.answers[spec.escalationId] ?? (reached(state.stage, spec.answeredBy) ? spec.answer : null);
}

/** The story's questions each have one asker: its words go to that conversation as written. */
function question(state: ChatState, spec: QuestionSpec): InlineQuestionItem {
  const answer = answerTo(state, spec);
  const answeredAt = answer === null ? null : minutesAgo(spec.minutes - 1);
  const recipients = [orgs.northlake, orgs.ridgeline];
  return {
    id: `esc:${spec.escalationId}`,
    at: minutesAgo(spec.minutes),
    type: "inline_question",
    escalation_id: spec.escalationId,
    thread_id: spec.threadId,
    question: spec.question,
    from_org: spec.from,
    status: answer === null ? "open" : "resolved",
    action: answer === null ? null : "approve",
    answer,
    askers: [],
    recipients,
    deliveries:
      answeredAt === null
        ? []
        : [{ thread_id: spec.threadId, org: spec.from, state: "as_written", at: answeredAt, reason: null }],
    answered_by: answer === null ? null : ref(members.alex),
    answered_at: answeredAt,
  };
}

/** The live demo's variants of the story (LIVE_DEMO_PLAN 3.5, 3.10, 3.11). */
export interface ChatOptions {
  /** The organization's send mode: true sends without review. */
  orgDefault?: boolean;
  /** This chat's override of it; null (the default) follows it. */
  override?: boolean | null;
  /** Claude's card goes out without review: "Sending…" at `drafted`, sent after. */
  auto?: boolean;
  /** Why an unreviewed send failed; the card reads "Not sent: …". */
  autoFailure?: string;
  /** Why Claude's last turn stopped. */
  turnFailure?: FailureReason;
  /** Why the system stopped Ridgeline's thread, from `sent` on. */
  stopReason?: FailureReason;
  /** How the first card was turned down: a message brings Claude's redraft. */
  rejected?: Rejection;
  /** Alex's last message, which Claude declined to respond to, and the refusal's category. */
  refused?: { text: string; category: string | null };
}

/** Mutable bits the routes change on top of the stage. */
export interface ChatState {
  stage: ChatStage;
  /** How the first card was turned down, if it was. */
  first: Rejection | null;
  /** How Claude's redraft was turned down, if it was. */
  second: Rejection | null;
  /** What Alex approved: their ticks and wording. */
  approved: ApproveCard | null;
  /** Answers given in the gallery, by escalation id. */
  answers: Readonly<Record<string, string>>;
  /** Messages sent in the gallery, after the stage's own. */
  sent: readonly UserTextItem[];
  /** The message Claude declined to respond to (`refused`), until Retry or Edit sends it again. */
  refused: UserTextItem | null;
  /** This chat's send-mode override, which `PATCH` changes. */
  override: boolean | null;
  options: ChatOptions;
  version: number;
}

export function initialState(stage: ChatStage, options: ChatOptions = {}): ChatState {
  return {
    stage,
    first: options.rejected ?? null,
    second: null,
    approved: null,
    answers: {},
    sent: [],
    refused:
      options.refused === undefined
        ? null
        : { ...user(id("7b0"), options.refused.text, 2), turn_id: TURN.refused },
    override: options.override ?? null,
    options,
    version: 1,
  };
}

function sendModeOf(state: ChatState): SendMode {
  const orgDefault = state.options.orgDefault ?? false;
  return { override: state.override, org_default: orgDefault, effective: state.override ?? orgDefault };
}

export function conversationAt(state: ChatState): ConversationDetail {
  const { stage } = state;
  const items: ChatItem[] = [user(MESSAGE_ID, OPENING_MESSAGE, 42)];

  items.push(
    thinking(
      TURN.search,
      "Split the ask into testing and a machined holder, then checked Henderson's coupon notes and budget.",
      42,
    ),
    tool(TURN.search, "search_org_knowledge", "Searched your organization's knowledge", "done", 42, {
      detail: "lattice coupons, CT holders, budget",
      outcome: "2 matches",
    }),
  );
  const [characterization, holder] = SEARCHES;
  items.push(
    tool(TURN.search, "search_network", "Searched the Harmony network", "done", 41, {
      detail: characterization.need,
      outcome: characterization.outcome,
    }),
  );
  if (stage === "working") {
    items.push(tool(TURN.search, "search_network", RUNNING_SEARCH_LABEL, "running", 41, { detail: holder.need, n: 1 }));
    return detail(state, items, null, true);
  }
  items.push(
    tool(TURN.search, "search_network", "Searched the Harmony network", "done", 41, {
      detail: holder.need,
      outcome: holder.outcome,
      n: 1,
    }),
    reply(TURN.search, 0, FIRST_REPLY, 40),
  );
  const { auto = false, autoFailure } = state.options;
  const { items: cards, pending } = drafts(state);
  items.push(...cards);
  if (autoFailure !== undefined) {
    items.push(reply(TURN.send, 0, `Couldn't send: ${autoFailure} Nothing went out.`, 39));
    return detail(state, items, null, false);
  }
  if (!reached(stage, "sent")) {
    // An unreviewed card never waits on the member: the turn goes on sending it.
    if (auto) return detail(state, items, null, true);
    return detail(state, items, pending, false);
  }

  items.push(
    { id: `req:${REQUEST_ID}`, at: minutesAgo(38), type: "request_status", request_id: REQUEST_ID },
    reply(TURN.send, 0, SENT_REPLY, 38),
  );
  // Questions arrive on their own: the representative routes them here, and internal
  // Claude only speaks again once the threads close.
  items.push(...QUESTIONS.filter((spec) => reached(stage, spec.askedAt)).map((spec) => question(state, spec)));
  if (reached(stage, "closed")) {
    items.push(
      tool(TURN.summary, "get_request_status", "Checked on the request", "done", 1),
      reply(TURN.summary, 0, CLOSING_SUMMARY, 1),
    );
  }
  if (state.refused !== null) items.push(state.refused);
  items.push(...state.sent);
  return detail(state, items, null, state.sent.length > 0);
}

function detail(
  state: ChatState,
  items: ChatItem[],
  pendingCardId: string | null,
  turnInFlight: boolean,
): ConversationDetail {
  return {
    id: CONVERSATION_ID,
    title: conversations[0]?.title ?? "",
    owner: ref(members.alex),
    items,
    pending_card_id: pendingCardId,
    queued_message_ids: [],
    turn_in_flight: turnInFlight,
    looking: false,
    turn_failure: turnInFlight ? null : state.refused !== null ? "refused" : (state.options.turnFailure ?? null),
    refusal:
      state.refused === null
        ? null
        : { turn_id: TURN.refused, category: state.options.refused?.category ?? null, retryable: true },
    send_mode: sendModeOf(state),
    version: state.version,
  };
}

// --- The request's status ------------------------------------------------------------

/** What each thread's newest message says, and each side's closing summary, as the story goes. */
export const LINES = {
  northlakeOpening:
    "Henderson Frame Lab needs CT scans and compression tests on 12 titanium lattice coupons, with results by Nov 6.",
  ridgelineOpening: "Henderson Frame Lab needs a holder made that stacks 12 lattice coupons for one CT session.",
  northlakeChecking: "Thanks. Let me check the compression calendar for a slot before Nov 6 and come back to you.",
  ridgelineAsks:
    "Happy to make it. Which tube should it use: aluminum, included, or carbon fiber for $85 more? We recommend carbon fiber for fine scans.",
  ridgelineQuote: "With a carbon-fiber tube the holder is $325 in total, delivered to the core on Mon Nov 2.",
  northlakeQuote:
    "Standard is $1,284 with reports by Fri Nov 13; priority, with the compression tests in the Wed Nov 4 block, is $1,434 with compression reports by Thu Nov 5 and CT reports by Fri Nov 6.",
  northlakeConfirmed: "Booked the priority option. Please drop the 12 coupons at our front desk by Tue Nov 3.",
  ridgelineConfirmed: "Confirmed. We'll start the holder today and deliver it to the core on Mon Nov 2.",
} as const;

export const SUMMARIES = {
  henderson: {
    northlake: "Booked CT and compression tests on 12 coupons for $1,434, compression in the Nov 4 priority block, reports by Nov 6.",
    ridgeline: "Ordered the stacked CT holder with a carbon-fiber tube for $325, delivered to the core on Nov 2.",
  },
  northlake: "Henderson Frame Lab booked CT and compression tests on 12 coupons for $1,434, in the Wed Nov 4 priority block.",
  ridgeline: "Henderson Frame Lab ordered one StackMount CT holder with a carbon-fiber tube, $325.",
} as const;

function recipient(
  base: CardRecipient,
  participant: RecipientStatus["participant"],
  threadId: string | null,
  extra: Partial<RecipientStatus> = {},
): RecipientStatus {
  return {
    org: base.org,
    fit: base.fit,
    reason: base.reason,
    // Every row with a thread went out; the story's only row without one, Calder, was
    // left out of the request.
    sent: threadId !== null,
    participant,
    thread_id: threadId,
    thread_state: threadId === null ? null : "active",
    waiting_on: null,
    latest_line: null,
    close_summary: null,
    last_activity_at: threadId === null ? null : minutesAgo(30),
    ...extra,
  };
}

function waitingOnAlex(spec: QuestionSpec): Partial<RecipientStatus> {
  return {
    thread_state: "awaiting_principal",
    waiting_on: { member: ref(members.alex), since: minutesAgo(spec.minutes) },
  };
}

function northlakeAt(state: ChatState, base: CardRecipient): RecipientStatus {
  const threadId = NORTHLAKE_THREAD_ID;
  const goAhead = NORTHLAKE_GO_AHEAD;
  if (reached(state.stage, "closed")) {
    return recipient(base, "closed", threadId, {
      thread_state: "closed",
      end_reason: "closed",
      closed_by: "requester",
      latest_line: LINES.northlakeConfirmed,
      close_summary: SUMMARIES.henderson.northlake,
      last_activity_at: minutesAgo(2),
    });
  }
  if (reached(state.stage, "go_ahead")) {
    return answerTo(state, goAhead) === null
      ? recipient(base, "needs_human", threadId, { ...waitingOnAlex(goAhead), latest_line: LINES.northlakeQuote })
      : recipient(base, "replied", threadId, { latest_line: answerTo(state, goAhead), last_activity_at: minutesAgo(0) });
  }
  if (reached(state.stage, "question")) {
    // Helen is deciding on Northlake's side; who that is stays Northlake's to know.
    return recipient(base, "needs_human", threadId, {
      thread_state: "awaiting_principal",
      latest_line: LINES.northlakeChecking,
    });
  }
  return recipient(base, "messaged", threadId, { latest_line: LINES.northlakeOpening });
}

function ridgelineAt(state: ChatState, base: CardRecipient): RecipientStatus {
  const threadId = RIDGELINE_THREAD_ID;
  const goAhead = RIDGELINE_GO_AHEAD;
  const { stopReason } = state.options;
  if (stopReason !== undefined) {
    // The system closed it before Ridgeline replied: no summary, only why.
    return recipient(base, "closed", threadId, {
      thread_state: "closed",
      end_reason: "stopped",
      stop_reason: stopReason,
      latest_line: LINES.ridgelineOpening,
      last_activity_at: minutesAgo(36),
    });
  }
  if (reached(state.stage, "closed")) {
    return recipient(base, "closed", threadId, {
      thread_state: "closed",
      end_reason: "closed",
      closed_by: "requester",
      latest_line: LINES.ridgelineConfirmed,
      close_summary: SUMMARIES.henderson.ridgeline,
      last_activity_at: minutesAgo(3),
    });
  }
  if (reached(state.stage, "go_ahead")) {
    return answerTo(state, goAhead) === null
      ? recipient(base, "needs_human", threadId, { ...waitingOnAlex(goAhead), latest_line: LINES.ridgelineQuote })
      : recipient(base, "replied", threadId, { latest_line: answerTo(state, goAhead), last_activity_at: minutesAgo(0) });
  }
  if (reached(state.stage, "question")) {
    const answer = answerTo(state, TUBE);
    return answer === null
      ? recipient(base, "needs_human", threadId, { ...waitingOnAlex(TUBE), latest_line: LINES.ridgelineAsks })
      : recipient(base, "replied", threadId, { latest_line: answer, last_activity_at: minutesAgo(0) });
  }
  return recipient(base, "messaged", threadId, { latest_line: LINES.ridgelineOpening });
}

export function requestAt(state: ChatState): RequestStatusView {
  const [northlake, ridgeline, calder] = cardRecipients as [CardRecipient, CardRecipient, CardRecipient];
  return {
    id: REQUEST_ID,
    title: REQUEST_TITLE,
    status: reached(state.stage, "closed") ? "closed" : "active",
    brief: state.approved?.brief ?? (state.first?.with === "message" ? redraftBrief : brief),
    conversation_id: CONVERSATION_ID,
    created_at: minutesAgo(38),
    screened,
    recipients: [northlakeAt(state, northlake), ridgelineAt(state, ridgeline), recipient(calder, "screened", null)],
    version: state.version,
  };
}

// --- Routes --------------------------------------------------------------------------

export interface ChatStory {
  routes: FixtureRoutes;
  /** Moves the story on to `stage`, as the workflow would between two reads. */
  advance: (stage: ChatStage) => void;
}

/**
 * The conversation at `stage`, and the writes that move it on. Everything else
 * (session, story, Recents, budget) comes from `shellRoutes()`.
 */
export function chatRoutes(stage: ChatStage = "drafted", options: ChatOptions = {}): FixtureRoutes {
  return chatStory(stage, options).routes;
}

/** `chatRoutes`, plus a way to move the story on from outside (a turn finishing, say). */
export function chatStory(stage: ChatStage = "drafted", options: ChatOptions = {}): ChatStory {
  let state = initialState(stage, options);
  let sentMessages = 0;
  const update = (patch: Partial<ChatState>) => {
    state = { ...state, ...patch, version: state.version + 1 };
  };
  const notFound = (what: string) => problem(404, "not_found", `No ${what} with that id.`);
  /** The card waiting on Alex, if any. */
  const pendingId = () => (options.auto === true ? null : drafts(state).pending);
  /** Turns the waiting card down; a message to the first one brings Claude's redraft. */
  const turnDown = (rejection: Rejection) => {
    update(state.first === null ? { first: rejection } : { second: rejection });
  };

  const routes: FixtureRoutes = {
    "POST /api/conversations": () => ({ id: CONVERSATION_ID, message_id: MESSAGE_ID }),
    "GET /api/conversations/{conversation_id}": ({ params }) =>
      params.conversation_id === CONVERSATION_ID ? conversationAt(state) : notFound("conversation"),
    "PATCH /api/conversations/{conversation_id}": ({ params, body }) => {
      if (params.conversation_id !== CONVERSATION_ID) return notFound("conversation");
      const override = isRecord(body) && typeof body.send_without_confirmation === "boolean" ? body.send_without_confirmation : null;
      update({ override });
      return sendModeOf(state);
    },
    "POST /api/conversations/{conversation_id}/messages": ({ params, body }) => {
      if (params.conversation_id !== CONVERSATION_ID) return notFound("conversation");
      const text = textOf(body);
      sentMessages += 1;
      const messageId = id(`7b${sentMessages}`);
      // While a card waits, a message turns it down and asks Claude for another.
      const cardId = pendingId();
      if (cardId !== null && state.stage === "drafted") {
        turnDown({ with: "message", text });
        return { message_id: messageId, outcome: "card_rejected", card_id: cardId };
      }
      update({ sent: [...state.sent, { ...user(messageId, text, 0), at: new Date().toISOString() }] });
      return { message_id: messageId, outcome: "queued" };
    },
    "POST /api/conversations/{conversation_id}/retry": ({ params, body }) => {
      if (params.conversation_id !== CONVERSATION_ID) return notFound("conversation");
      if (state.refused === null) return problem(409, "nothing_to_retry", "There's no declined message to retry.");
      sentMessages += 1;
      const messageId = id(`7b${sentMessages}`);
      const text = textOf(body) || state.refused.text;
      update({ refused: null, sent: [...state.sent, { ...user(messageId, text, 0), at: new Date().toISOString() }] });
      return { message_ids: [messageId] };
    },
    "POST /api/cards/{card_id}/approve": ({ params, body }) => {
      if (state.stage !== "drafted" || params.card_id !== pendingId()) {
        return problem(409, "card_not_pending", "This card was already answered.");
      }
      update({ stage: "sent", approved: approveOf(body) });
      return { card_id: params.card_id, status: "approved", request_id: REQUEST_ID };
    },
    "POST /api/cards/{card_id}/reject": ({ params, body }) => {
      if (state.stage !== "drafted" || params.card_id !== pendingId()) {
        return problem(409, "card_not_pending", "This card was already answered.");
      }
      const reason = isRecord(body) && typeof body.feedback === "string" ? body.feedback : null;
      turnDown({ with: "decline", text: reason });
      return { card_id: params.card_id, status: "rejected" };
    },
    "GET /api/requests/{request_id}": ({ params }) =>
      params.request_id === REQUEST_ID && reached(state.stage, "sent") ? requestAt(state) : notFound("request"),
    "POST /api/escalations/{escalation_id}/resolve": ({ params, body }) => {
      const spec = QUESTIONS.find((candidate) => candidate.escalationId === params.escalation_id);
      if (spec === undefined) return notFound("escalation");
      if (answerTo(state, spec) !== null) {
        return problem(409, "escalation_not_open", "Already answered.", { resolved_by: ref(members.alex) });
      }
      const answers = { ...state.answers, [spec.escalationId]: textOf(body) };
      // Once Alex has said yes to both, both representatives confirm and close.
      const both = [NORTHLAKE_GO_AHEAD_ID, RIDGELINE_GO_AHEAD_ID].every((escalationId) => escalationId in answers);
      update({ answers, ...(both ? { stage: "closed" as const } : {}) });
      return { escalation_id: spec.escalationId, action: "approve", message_id: id(`7c${QUESTIONS.indexOf(spec) + 1}`) };
    },
  };
  return {
    routes,
    advance: (next) => {
      update({ stage: next });
    },
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** The approve body's ticks and wording, as the API would read them. */
function approveOf(body: unknown): ApproveCard | null {
  if (!isRecord(body)) return null;
  const { recipients, brief: edited, asks } = body;
  return {
    ...(Array.isArray(recipients) ? { recipients: recipients.filter((each) => typeof each === "string") } : {}),
    ...(typeof edited === "string" ? { brief: edited } : {}),
    ...(isRecord(asks)
      ? { asks: Object.fromEntries(Object.entries(asks).filter((entry): entry is [string, string] => typeof entry[1] === "string")) }
      : {}),
  };
}

function textOf(body: unknown): string {
  return isRecord(body) && typeof body.text === "string" ? body.text : "";
}

// --- Stream ----------------------------------------------------------------------------

/** A `stream.chat_delta` frame for Alex's conversation. */
export function chatDeltaEnvelope(turnId: string, attempt: number, text: string): EventEnvelope {
  return {
    world_id: WORLD_ID,
    seq: null,
    created_at: new Date().toISOString(),
    scope: { kind: "member", org_id: orgs.henderson.id, member_id: members.alex.id, thread_id: null, session_id: null },
    event: {
      type: "stream.chat_delta",
      org_id: orgs.henderson.id,
      member_id: members.alex.id,
      conversation_id: CONVERSATION_ID,
      turn_id: turnId,
      attempt,
      text,
    },
  };
}
