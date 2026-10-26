/**
 * The chat's fixtures: Alex's conversation from the harmony-demo story
 * (REVISION_UNSTRUCTURED.md §10), at each stage from the first search to both threads
 * closed, plus the request status and the routes that move it along. The routes are
 * stateful, so approving the card or answering a question in the gallery really
 * advances the chat.
 */
import type { EventEnvelope } from "../api/types";
import type {
  AssistantTextItem,
  CardRecipient,
  ChatItem,
  ConversationDetail,
  InlineQuestionItem,
  MemberRef,
  OutreachCardItem,
  RecipientStatus,
  RequestStatusView,
  ScreenedCounts,
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
export const REQUEST_ID = id("6a1");
export const NORTHLAKE_THREAD_ID = id("6b1");
export const RIDGELINE_THREAD_ID = id("6b2");
export const SEAL_QUESTION_ID = id("6e2");
export const NORTHLAKE_GO_AHEAD_ID = id("6e3");
export const RIDGELINE_GO_AHEAD_ID = id("6e4");

const MESSAGE_ID = id("7a1");
/** Alex sends the walkthrough's suggested message as it stands. */
export const OPENING_MESSAGE = story.suggested_message ?? "";

export const TURN = {
  search: id("8d1"),
  send: id("8d2"),
  summary: id("8d3"),
} as const;

function minutesAgo(minutes: number): string {
  return new Date(Date.parse(FIXTURE_NOW) - minutes * 60_000).toISOString();
}

function ref(member: { id: string; slug: string; name: string }): MemberRef {
  return { id: member.id, slug: member.slug, name: member.name };
}

// --- The brief and the recipients ----------------------------------------------------

export const brief = [
  "XRD and SEM/EDS on 12 air-sensitive Na-ion cathode powders, plus an airtight holder",
  "",
  "Henderson Lab needs powder XRD and SEM/EDS on 12 layered-oxide cathode powders (Na–Mn–Fe–Ni–O family), with results by Nov 6. The powders are stored under argon and take up moisture within minutes in air, so they need air-free handling from unpacking to measurement.",
  "",
  "We also need an airtight powder holder for a Bruker D8 dome mount, so the samples can go to the diffractometer without exposure.",
  "",
  "Please reply with what you can do, the price and the turnaround. The samples can ship to arrive by Nov 3.",
].join("\n");

/** The request's name in views: the brief's first line (`agents.models.brief_title`). */
export const REQUEST_TITLE = brief.split("\n")[0] ?? "";

export const cardRecipients: CardRecipient[] = [
  {
    org: orgs.northlake,
    fit: "strong",
    reason: "Powder XRD and SEM/EDS, and they load air-sensitive powders in their own glovebox.",
    selected: true,
  },
  {
    org: orgs.ridgeline,
    fit: "strong",
    reason: "Machines airtight holders for Bruker D8 dome mounts in about 4 business days.",
    selected: true,
  },
  {
    org: orgs.calder,
    fit: "no",
    reason: "Computation only: DFT screening, no lab instruments.",
    selected: true,
  },
];

export const screened: ScreenedCounts = { total: 14, strong: 2, possible: 4, no: 8, unknown: 0 };

/** What Alex answers, unless a gallery visitor types something else. */
export const ANSWERS = {
  seal: "Kalrez, please. The powders may sit under argon for a few weeks.",
  northlake: "Yes, go ahead with Northlake.",
  ridgeline: "Yes, go ahead with Ridgeline, with the Kalrez seal.",
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
  detail: string | null,
  status: ToolActivityItem["status"],
  minutes: number,
): ToolActivityItem {
  return {
    id: `tool:${turnId}:${name}`,
    at: minutesAgo(minutes),
    type: "tool_activity",
    tool: name,
    label,
    detail,
    status,
    turn_id: turnId,
  };
}

export const FIRST_REPLY = [
  "I screened the network and found two organizations that fit, plus one near miss:",
  "",
  "- **Northlake Characterization Core** runs powder XRD and SEM/EDS, and loads air-sensitive powders in its own glovebox.",
  "- **Ridgeline Fabrication** machines airtight holders for the Bruker D8 dome mount in about 4 business days.",
  "- **Calder Computational** only does computation, so I've left it unticked.",
  "",
  "The brief tells them the sample count, the Nov 6 date and that the powders are air-sensitive. It leaves out the dopant compositions and your budget. Here it is for your OK.",
].join("\n");

export const CLOSING_SUMMARY = [
  "Both are booked:",
  "",
  "- **Northlake** runs XRD and SEM/EDS on all 12 powders for **$1,410**, with results by Nov 6. Ship the powders sealed under argon to arrive by Nov 3.",
  "- **Ridgeline** makes the airtight holder with a Kalrez seal for **$325**, shipping in 4 business days.",
  "",
  "That's **$1,735** in total, inside your $2,000 budget.",
].join("\n");

function outreachCard(stage: ChatStage, feedback: string | null): OutreachCardItem {
  const sent = reached(stage, "sent");
  return {
    id: `card:${OUTREACH_CARD_ID}`,
    at: minutesAgo(40),
    type: "outreach_card",
    card_id: OUTREACH_CARD_ID,
    status: sent ? "approved" : feedback !== null ? "rejected" : "pending",
    brief,
    // Like `domain/conversations.py`: once approved, those the member left out aren't selected.
    recipients: sent ? cardRecipients.map((r) => ({ ...r, selected: r.fit !== "no" })) : cardRecipients,
    screened,
    feedback,
    request_id: sent ? REQUEST_ID : null,
    version: sent || feedback !== null ? 2 : 1,
  };
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

const SEAL: QuestionSpec = {
  escalationId: SEAL_QUESTION_ID,
  threadId: RIDGELINE_THREAD_ID,
  from: orgs.ridgeline,
  question:
    "Ridgeline asks which seal material the holder should use: Viton, which is included, or Kalrez for $85 more, which they recommend for long storage under argon. Which would you like?",
  askedAt: "question",
  answeredBy: "go_ahead",
  answer: ANSWERS.seal,
  minutes: 18,
};

const NORTHLAKE_GO_AHEAD: QuestionSpec = {
  escalationId: NORTHLAKE_GO_AHEAD_ID,
  threadId: NORTHLAKE_THREAD_ID,
  from: orgs.northlake,
  question:
    "Northlake proposes $1,410 for XRD and SEM/EDS on all 12 powders, with results by Nov 6. The powders would need to arrive by Nov 3, sealed under argon. Go ahead?",
  askedAt: "go_ahead",
  answeredBy: "closed",
  answer: ANSWERS.northlake,
  minutes: 7,
};

const RIDGELINE_GO_AHEAD: QuestionSpec = {
  escalationId: RIDGELINE_GO_AHEAD_ID,
  threadId: RIDGELINE_THREAD_ID,
  from: orgs.ridgeline,
  question: "Ridgeline quotes $325 for the airtight holder with a Kalrez seal, shipping in 4 business days. Go ahead?",
  askedAt: "go_ahead",
  answeredBy: "closed",
  answer: ANSWERS.ridgeline,
  minutes: 6,
};

const QUESTIONS: readonly QuestionSpec[] = [SEAL, NORTHLAKE_GO_AHEAD, RIDGELINE_GO_AHEAD];

/** The member's answer to a question, or null while it's open. */
function answerTo(state: ChatState, spec: QuestionSpec): string | null {
  return state.answers[spec.escalationId] ?? (reached(state.stage, spec.answeredBy) ? spec.answer : null);
}

function question(state: ChatState, spec: QuestionSpec): InlineQuestionItem {
  const answer = answerTo(state, spec);
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
  };
}

/** Mutable bits the routes change on top of the stage. */
export interface ChatState {
  stage: ChatStage;
  /** Rejection feedback on the outreach card. */
  feedback: string | null;
  /** Answers given in the gallery, by escalation id. */
  answers: Readonly<Record<string, string>>;
  /** Messages sent in the gallery, after the stage's own. */
  sent: readonly UserTextItem[];
  version: number;
}

export function initialState(stage: ChatStage): ChatState {
  return { stage, feedback: null, answers: {}, sent: [], version: 1 };
}

export function conversationAt(state: ChatState): ConversationDetail {
  const { stage } = state;
  const items: ChatItem[] = [user(MESSAGE_ID, OPENING_MESSAGE, 42)];

  items.push(
    thinking(
      TURN.search,
      "Split the ask into characterization and a machined holder, then checked Henderson's sample notes and budget.",
      42,
    ),
    tool(TURN.search, "search_org_knowledge", "Searched Henderson Lab's knowledge", "Project NAC-12 · Budget", "done", 42),
  );
  if (stage === "working") {
    items.push(tool(TURN.search, "search_network", "Searching the Harmony network", null, "running", 41));
    return detail(state, items, null, true);
  }
  items.push(
    tool(TURN.search, "search_network", "Searched the Harmony network", `${screened.total} organizations`, "done", 41),
    reply(TURN.search, 0, FIRST_REPLY, 40),
    outreachCard(stage, state.feedback),
  );
  if (!reached(stage, "sent")) {
    return detail(state, items, state.feedback === null ? OUTREACH_CARD_ID : null, state.feedback !== null);
  }

  items.push(
    tool(TURN.send, "send_network_request", "Sent the network request", "2 organizations", "done", 38),
    { id: `req:${REQUEST_ID}`, at: minutesAgo(38), type: "request_status", request_id: REQUEST_ID },
    reply(
      TURN.send,
      0,
      "Sent to Northlake and Ridgeline. Your representative will work it out with theirs, and anything that needs you will come back here.",
      38,
    ),
  );
  // Questions arrive on their own: the representative routes them here, and internal
  // Claude only speaks again once the threads close.
  items.push(...QUESTIONS.filter((spec) => reached(stage, spec.askedAt)).map((spec) => question(state, spec)));
  if (reached(stage, "closed")) {
    items.push(
      tool(TURN.summary, "get_request_status", "Checked the request", "2 closed", "done", 1),
      reply(TURN.summary, 0, CLOSING_SUMMARY, 1),
    );
  }
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
    turn_failed: false,
    version: state.version,
  };
}

// --- The request's status ------------------------------------------------------------

/** What each thread's newest message says, and each side's closing summary, as the story goes. */
export const LINES = {
  northlakeOpening:
    "Henderson Lab needs powder XRD and SEM/EDS on 12 air-sensitive Na-ion cathode powders, with results by Nov 6.",
  ridgelineOpening:
    "Henderson Lab needs an airtight powder holder for a Bruker D8 dome mount, for air-sensitive powders.",
  northlakeChecking: "Thanks. Let me check the XRD calendar for a slot before Nov 6 and come back to you.",
  ridgelineAsks:
    "Happy to make it. Which seal should it use: Viton, included, or Kalrez for $85 more? We recommend Kalrez for long storage.",
  ridgelineQuote: "With a Kalrez seal the holder is $325 in total, and it ships in 4 business days.",
  northlakeQuote:
    "We can meet Nov 6: XRD on the morning of Wed Nov 4 and SEM/EDS from Nov 3, $1,410 for all 12 powders.",
  northlakeConfirmed: "Confirmed. Please ship the 12 powders sealed under argon to arrive by Tue Nov 3.",
  ridgelineConfirmed: "Confirmed. We'll start the holder today and ship it in 4 business days.",
} as const;

export const SUMMARIES = {
  henderson: {
    northlake: "Booked XRD and SEM/EDS on 12 powders for $1,410, results by Nov 6. Ship under argon by Nov 3.",
    ridgeline: "Ordered the airtight holder with a Kalrez seal for $325, shipping in 4 business days.",
  },
  northlake: "Henderson Lab booked XRD and SEM/EDS on 12 powders for $1,410, using the Wed Nov 4 slot.",
  ridgeline: "Henderson Lab ordered one airtight D8 holder with a Kalrez seal, $325.",
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
  if (reached(state.stage, "closed")) {
    return recipient(base, "closed", threadId, {
      thread_state: "closed",
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
    const answer = answerTo(state, SEAL);
    return answer === null
      ? recipient(base, "needs_human", threadId, { ...waitingOnAlex(SEAL), latest_line: LINES.ridgelineAsks })
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
    brief,
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
 * (session, story, Recents) comes from `shellRoutes()`.
 */
export function chatRoutes(stage: ChatStage = "drafted"): FixtureRoutes {
  return chatStory(stage).routes;
}

/** `chatRoutes`, plus a way to move the story on from outside (a turn finishing, say). */
export function chatStory(stage: ChatStage = "drafted"): ChatStory {
  let state = initialState(stage);
  let sentMessages = 0;
  const update = (patch: Partial<ChatState>) => {
    state = { ...state, ...patch, version: state.version + 1 };
  };
  const notFound = (what: string) => problem(404, "not_found", `No ${what} with that id.`);
  const cardPending = () => state.stage === "drafted" && state.feedback === null;

  const routes: FixtureRoutes = {
    "POST /api/conversations": () => ({ id: CONVERSATION_ID, message_id: MESSAGE_ID }),
    "GET /api/conversations/{conversation_id}": ({ params }) =>
      params.conversation_id === CONVERSATION_ID ? conversationAt(state) : notFound("conversation"),
    "POST /api/conversations/{conversation_id}/messages": ({ params, body }) => {
      if (params.conversation_id !== CONVERSATION_ID) return notFound("conversation");
      const text = textOf(body);
      sentMessages += 1;
      const messageId = id(`7b${sentMessages}`);
      // While the outreach card waits, a message is the card's rejection feedback.
      if (cardPending()) {
        update({ feedback: text });
        return { message_id: messageId, outcome: "card_rejected", card_id: OUTREACH_CARD_ID };
      }
      update({ sent: [...state.sent, { ...user(messageId, text, 0), at: new Date().toISOString() }] });
      return { message_id: messageId, outcome: "queued" };
    },
    "POST /api/cards/{card_id}/approve": ({ params }) => {
      if (params.card_id !== OUTREACH_CARD_ID || !cardPending()) {
        return problem(409, "card_not_pending", "This card was already answered.");
      }
      update({ stage: "sent" });
      return { card_id: OUTREACH_CARD_ID, status: "approved", request_id: REQUEST_ID };
    },
    "POST /api/cards/{card_id}/reject": ({ params, body }) => {
      if (params.card_id !== OUTREACH_CARD_ID || !cardPending()) {
        return problem(409, "card_not_pending", "This card was already answered.");
      }
      const feedback = isRecord(body) && typeof body.feedback === "string" ? body.feedback : "";
      update({ feedback });
      return { card_id: OUTREACH_CARD_ID, status: "rejected" };
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
