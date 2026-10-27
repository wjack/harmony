/**
 * Network view fixtures: the harmony-demo network (REVISION_UNSTRUCTURED.md §10) at five
 * points of the suggested story, plus a big-run world.
 *
 * Shapes follow `GET /api/network` as E4's route builds them (api/routes/network.py):
 * - only the request's recipients carry `participant`; everyone else is a bystander;
 * - a recipient whose thread hasn't opened yet is `screened`;
 * - the lens and the requester's members see every recipient, with `fit` and the
 *   story's `suggested` marks; a recipient's members see only their own org and its
 *   threads, with neither (§11.6); with no request in focus nobody gets the marks;
 * - a desk member lists their own chats' requests and those with a thread they can
 *   see (an admin of a party org, or a `thread_access` row), and only those threads
 *   are detailed (latest line, turn count);
 * - a closed thread's summary shows only to the closer's own org;
 * - in the desk world `waiting_on` names only the acting member's own org; the lens
 *   names anyone.
 *
 * The story keeps Calder on the request as a screened recipient (§10: "Calder
 * Screened"). Each stage is written once, as the whole truth, and scoped to the acting
 * member the way the API scopes it. Times are relative to when the module loads, so
 * "waiting 4m" reads as it would live.
 */
import type { EndReason, MemberDirectory, MemberRef, MemberView, OrgRef, SessionView } from "../api/types";
import type { FailureReason } from "../chat/failureCopy";
import type { RequestStatusView } from "../chat/types";
import type { NetworkEdge, NetworkNode, NetworkRequestRef, NetworkView, ParticipantStatus } from "../network/types";
import { brief, LINES, REQUEST_TITLE, SUMMARIES } from "./chat";
import { json, problem, type FixtureRequest, type FixtureRoutes } from "./index";
import { WORLD_ID, conversations, members, memberDirectory, orgs as castOrgs, shellRoutes } from "./shell";

const LOADED_AT = Date.now();

function minutesAgo(minutes: number): string {
  return new Date(LOADED_AT - minutes * 60_000).toISOString();
}

function ref(member: MemberView): MemberRef {
  return { id: member.id, slug: member.slug, name: member.name };
}

function id(prefix: string, index: number): string {
  return `0b1d7a3e-0000-4000-8000-000000000${prefix}${String(index).padStart(2, "0")}`;
}

function org(index: number, slug: string, name: string): OrgRef {
  return { id: id("4", index), slug, name };
}

/** The scenario's orgs and their categories (`orgs.kind`). */
export const networkOrgs: readonly { org: OrgRef; kind: string; featured: boolean }[] = [
  { org: castOrgs.henderson, kind: "research", featured: true },
  { org: castOrgs.northlake, kind: "characterization", featured: true },
  { org: castOrgs.ridgeline, kind: "fabrication", featured: true },
  { org: castOrgs.calder, kind: "computation", featured: true },
  { org: castOrgs.vantage, kind: "procurement", featured: true },
  { org: org(1, "bayside", "Bayside Analytical"), kind: "characterization", featured: false },
  { org: org(2, "summit", "Summit Glovebox Services"), kind: "logistics", featured: false },
  { org: org(3, "ostrander", "Ostrander Materials Lab"), kind: "characterization", featured: false },
  { org: org(4, "pinecrest", "Pinecrest Precursors"), kind: "synthesis", featured: false },
  { org: org(5, "harbor", "Harbor Pilot Line"), kind: "fabrication", featured: false },
  { org: org(6, "meridian", "Meridian Thermal"), kind: "characterization", featured: false },
  { org: org(7, "ridgeway", "Ridgeway Foundation for Energy Materials"), kind: "funder", featured: false },
  { org: org(8, "lumen", "Lumen Data Commons"), kind: "data", featured: false },
  { org: org(9, "granite", "Granite Precision Machining"), kind: "fabrication", featured: false },
  { org: org(10, "tidewater", "Tidewater Electrochem"), kind: "testing", featured: false },
];

/** The story's suggested recipients, flagged for viewers who see every recipient. */
const STORY_RECIPIENTS: ReadonlySet<string> = new Set(["northlake", "ridgeline", "calder"]);

export const REQUEST_ID = id("5", 1);
const OLDER_REQUEST_ID = id("5", 2);
export const THREAD_NORTHLAKE = id("6", 1);
export const THREAD_RIDGELINE = id("6", 2);
/** The chat Alex sent the request from: Ridgeline's seal question waits there. */
export const REQUEST_CONVERSATION_ID = conversations[0]?.id ?? id("2", 1);

const request: NetworkRequestRef = {
  id: REQUEST_ID,
  title: REQUEST_TITLE,
  requester: castOrgs.henderson,
  status: "active",
  created_at: minutesAgo(14),
};

const olderRequest: NetworkRequestRef = {
  id: OLDER_REQUEST_ID,
  title: "Coin-cell cycling capacity for November",
  requester: castOrgs.henderson,
  status: "closed",
  created_at: minutesAgo(60 * 26),
};

// --- One stage, as the whole truth --------------------------------------------------

interface Recipient {
  participant: ParticipantStatus;
  fit: NonNullable<NetworkNode["fit"]>;
  /** Who its thread waits on, on either side, and for how long. */
  waiting?: { member: MemberView; minutes: number };
}

interface Thread {
  id: string;
  from: OrgRef;
  to: OrgRef;
  state: NetworkEdge["state"];
  turns: number;
  minutes: number;
  /** The newest message, as one line. */
  latest: string;
  /** Each side's closing summary, by org id: only its own org reads it. */
  summaries?: Readonly<Record<string, string>>;
  /** Why the system, not a person, closed it (LIVE_DEMO_PLAN 3.5). */
  stopReason?: FailureReason;
  /** Members with a `thread_access` row; a party org's admins see it regardless. */
  access: readonly string[];
}

interface Stage {
  requester: OrgRef | null;
  /** Whose chat sent the stage's requests: they list them whatever threads they see. */
  owner: MemberView | null;
  requests: NetworkRequestRef[];
  requestId: string | null;
  recipients: Readonly<Record<string, Recipient>>;
  threads: readonly Thread[];
  orgs: readonly { org: OrgRef; kind: string; featured: boolean }[];
  worldId: string;
}

/** Who reads the view: a member at their desk, or null for the lens (both sides). */
type Reader = MemberView | null;

const ENDED: ReadonlySet<NetworkEdge["state"]> = new Set(["closed", "expired"]);

function view(stage: Stage, reader: Reader): NetworkView {
  const party = (thread: Thread) =>
    reader === null || thread.from.id === reader.org.id || thread.to.id === reader.org.id;
  const sees = (thread: Thread) =>
    reader === null || (party(thread) && (reader.role === "admin" || thread.access.includes(reader.id)));
  const listed = stage.requests.filter(
    (entry) =>
      reader === null || stage.owner?.id === reader.id || (entry.id === stage.requestId && stage.threads.some(sees)),
  );
  const focused = listed.some((entry) => entry.id === stage.requestId) ? stage.requestId : null;
  const full = focused !== null && (reader === null || reader.org.id === stage.requester?.id);
  // Whom else the requester asked, and how those threads stand, is the requester's (§11.6).
  const shown = (entry: OrgRef) => full || (focused !== null && entry.id === reader?.org.id);
  const threads = focused === null ? [] : full ? stage.threads : stage.threads.filter(party);
  const nodes = stage.orgs.map(({ org: entry, kind, featured }): NetworkNode => {
    const recipient = shown(entry) ? stage.recipients[entry.slug] : undefined;
    const waiting = recipient?.waiting;
    const thread = stage.threads.find((candidate) => candidate.to.id === entry.id);
    // Who a thread waits on is the member's own org's, and only on a thread they can open.
    const named =
      waiting !== undefined &&
      (reader === null || (waiting.member.org.id === reader.org.id && thread !== undefined && sees(thread)));
    const requester = focused !== null && entry.id === stage.requester?.id;
    return {
      org: entry,
      kind,
      featured,
      role: requester ? "requester" : recipient === undefined ? "bystander" : "recipient",
      participant: recipient?.participant ?? null,
      fit: full ? (recipient?.fit ?? null) : null,
      waiting_on: named ? { member: ref(waiting.member), since: minutesAgo(waiting.minutes) } : null,
      suggested: full && STORY_RECIPIENTS.has(entry.slug),
    };
  });
  const edges = threads.map(
    (thread): NetworkEdge => ({
      thread_id: thread.id,
      from_org_id: thread.from.id,
      to_org_id: thread.to.id,
      state: thread.state,
      latest_line: sees(thread) ? thread.latest : null,
      turn_count: sees(thread) ? thread.turns : null,
      close_summary: reader === null ? null : (thread.summaries?.[reader.org.id] ?? null),
      stop_reason: thread.stopReason ?? null,
      end_reason: endReason(thread),
      closed_by: endReason(thread) === "closed" ? "responder" : null,
      last_activity_at: minutesAgo(thread.minutes),
    }),
  );
  return {
    world_id: stage.worldId,
    requests: listed,
    request_id: focused,
    nodes,
    edges,
    counts: {
      orgs: nodes.length,
      threads: edges.length,
      active: edges.filter((edge) => !ENDED.has(edge.state)).length,
      closed: edges.filter((edge) => ENDED.has(edge.state)).length,
    },
  };
}

/** How a thread's run ended: both sides going quiet (as a seeded expired thread reads), a stop or a close. */
function endReason(thread: Thread): EndReason | null {
  if (thread.state === "expired") return "quiet";
  if (thread.state !== "closed") return null;
  return thread.stopReason === undefined ? "closed" : "stopped";
}

function storyThread(
  to: OrgRef,
  state: NetworkEdge["state"],
  turns: number,
  minutes: number,
  latest: string,
  summaries?: Readonly<Record<string, string>>,
  stopReason?: FailureReason,
): Thread {
  const threadId = to.id === castOrgs.northlake.id ? THREAD_NORTHLAKE : THREAD_RIDGELINE;
  // Alex's representative routes the requester side to him.
  return {
    id: threadId,
    from: castOrgs.henderson,
    to,
    state,
    turns,
    minutes,
    latest,
    summaries,
    stopReason,
    access: [members.alex.id],
  };
}

function deskStage(partial: Partial<Stage> & Pick<Stage, "recipients" | "threads">): Stage {
  return {
    requester: castOrgs.henderson,
    owner: members.alex,
    requests: [request, olderRequest],
    requestId: REQUEST_ID,
    orgs: networkOrgs,
    worldId: WORLD_ID,
    ...partial,
  };
}

/** Calder stays on the request, screened as a near miss and never messaged. */
const calder: Recipient = { participant: "screened", fit: "no" };

const STAGES = {
  /** Before Alex has sent anything: no request, so nobody gets the story's marks. */
  before: deskStage({ requester: null, requests: [], requestId: null, recipients: {}, threads: [] }),

  /** Right after approval: Northlake's thread is open; Ridgeline's opens next. */
  messaging: deskStage({
    recipients: {
      northlake: { participant: "messaged", fit: "strong" },
      ridgeline: { participant: "screened", fit: "strong" },
      calder,
    },
    threads: [storyThread(castOrgs.northlake, "active", 1, 1, LINES.northlakeOpening)],
  }),

  /** Ridgeline asks which seal material; Henderson's representative asks Alex. */
  question: deskStage({
    recipients: {
      northlake: { participant: "replied", fit: "strong" },
      ridgeline: { participant: "needs_human", fit: "strong", waiting: { member: members.alex, minutes: 3 } },
      calder,
    },
    threads: [
      storyThread(castOrgs.northlake, "active", 2, 1, LINES.northlakeChecking),
      storyThread(castOrgs.ridgeline, "awaiting_principal", 2, 3, LINES.ridgelineAsks),
    ],
  }),

  /** The story's middle: Northlake's representative asked Helen; Ridgeline has quoted. */
  needs_human: deskStage({
    recipients: {
      northlake: { participant: "needs_human", fit: "strong", waiting: { member: members.helen, minutes: 4 } },
      ridgeline: { participant: "replied", fit: "strong" },
      calder,
    },
    threads: [
      storyThread(castOrgs.northlake, "awaiting_principal", 3, 4, LINES.northlakeChecking),
      storyThread(castOrgs.ridgeline, "active", 4, 2, LINES.ridgelineQuote),
    ],
  }),

  /**
   * Today's live budget ran out mid-story: Ridgeline's thread closed before it replied,
   * with no summary, while Northlake's goes on.
   */
  stopped: deskStage({
    recipients: {
      northlake: { participant: "replied", fit: "strong" },
      ridgeline: { participant: "closed", fit: "strong" },
      calder,
    },
    threads: [
      storyThread(castOrgs.northlake, "active", 2, 1, LINES.northlakeChecking),
      storyThread(castOrgs.ridgeline, "closed", 1, 6, LINES.ridgelineOpening, undefined, "daily_budget"),
    ],
  }),

  /** The end of the story: both threads closed, each side with its own summary. */
  closed: deskStage({
    requests: [{ ...request, status: "closed" }, olderRequest],
    recipients: {
      northlake: { participant: "closed", fit: "strong" },
      ridgeline: { participant: "closed", fit: "strong" },
      calder,
    },
    threads: [
      storyThread(castOrgs.northlake, "closed", 8, 1, LINES.northlakeConfirmed, {
        [castOrgs.henderson.id]: SUMMARIES.henderson.northlake,
        [castOrgs.northlake.id]: SUMMARIES.northlake,
      }),
      storyThread(castOrgs.ridgeline, "closed", 7, 2, LINES.ridgelineConfirmed, {
        [castOrgs.henderson.id]: SUMMARIES.henderson.ridgeline,
        [castOrgs.ridgeline.id]: SUMMARIES.ridgeline,
      }),
    ],
  }),
} satisfies Record<string, Stage>;

export type NetworkStage = keyof typeof STAGES;

/** `GET /api/network` at `stage`, as `actor` reads it in the desk world. */
export function deskNetwork(stage: NetworkStage, actor: MemberView = members.alex): NetworkView {
  return view(STAGES[stage], actor);
}

export const networkBefore = deskNetwork("before");
export const networkMessaging = deskNetwork("messaging");
export const networkQuestion = deskNetwork("question");
export const networkNeedsHuman = deskNetwork("needs_human");
export const networkClosed = deskNetwork("closed");
export const networkStopped = deskNetwork("stopped");

// --- The big run (the network world) ------------------------------------------------

const NETWORK_WORLD_ID = "0b1d7a3e-5c2f-4e8a-9a51-000000000002";

const BIG_RUN_ORGS: readonly [string, string, string][] = [
  ["anvil", "Anvil Ceramics Lab", "synthesis"],
  ["aurora", "Aurora Beamline", "characterization"],
  ["basalt", "Basalt Compute Co-op", "computation"],
  ["cobalt-free", "Cobalt-Free Materials", "synthesis"],
  ["delta", "Delta Pilot Plant", "fabrication"],
  ["ember", "Ember Thermal Analysis", "characterization"],
  ["fjord", "Fjord Electrochem", "testing"],
  ["glacier", "Glacier Data Trust", "data"],
  ["helix", "Helix Simulation Group", "computation"],
  ["iron-bay", "Iron Bay Foundry", "fabrication"],
  ["juniper", "Juniper Cell Test", "testing"],
  ["kestrel", "Kestrel XRD Services", "characterization"],
  ["loam", "Loam Precursors", "synthesis"],
  ["mesa", "Mesa Microscopy", "characterization"],
  ["nimbus", "Nimbus Cost Modeling", "computation"],
  ["onyx", "Onyx Coatings", "fabrication"],
  ["prairie", "Prairie Energy Fund", "funder"],
  ["quartz", "Quartz Spectroscopy", "characterization"],
  ["redwood", "Redwood Pouch Cells", "fabrication"],
  ["sable", "Sable Sodium Salts", "synthesis"],
];

const bigRunOrgs = [
  ...networkOrgs,
  ...BIG_RUN_ORGS.map(([slug, name, kind], index) => ({ org: org(20 + index, slug, name), kind, featured: false })),
];

function bigRunOrg(slug: string): OrgRef {
  const found = bigRunOrgs.find((entry) => entry.org.slug === slug);
  if (found === undefined) throw new Error(`no big-run org ${slug}`);
  return found.org;
}

function networkMember(index: number, slug: string, name: string, role: MemberView["role"], memberOrg: OrgRef): MemberView {
  return { id: id("9", index), slug, name, title: null, role, org: memberOrg };
}

// Each world has its own member rows: the big run's Alex isn't the desk's.
const networkAlex = networkMember(1, "alex", "Alex Chen", "member", castOrgs.henderson);
const networkMembers: readonly MemberView[] = [
  networkAlex,
  networkMember(2, "maya", "Prof. Maya Henderson", "admin", castOrgs.henderson),
  networkMember(3, "helen", "Dr. Helen Ward", "admin", castOrgs.northlake),
  networkMember(4, "ines", "Ines Moreau", "admin", bigRunOrg("delta")),
  networkMember(5, "tomas", "Tomas Reyes", "member", bigRunOrg("juniper")),
];

export const networkDirectory: MemberDirectory = { featured: networkMembers.slice(0, 3), all: networkMembers.slice(3) };

type Contacted = Exclude<ParticipantStatus, "screened">;

const BIG_STATUSES: readonly Contacted[] = [
  "closed",
  "replied",
  "messaged",
  "closed",
  "needs_human",
  "replied",
  "closed",
  "messaged",
  "replied",
  "replied",
];

const STATE_FOR: Record<Contacted, NetworkEdge["state"]> = {
  messaged: "active",
  replied: "active",
  needs_human: "awaiting_principal",
  closed: "closed",
};

// What the lens reads as each thread's newest line, by where the thread stands.
const BIG_LINES: Record<Contacted, string> = {
  messaged: "Henderson Lab is looking for partners to design and validate a low-cost Na-ion cathode.",
  replied: "We can take part. Here's what we'd run, and roughly when.",
  needs_human: "Let me check that with our team before I commit to a date.",
  closed: "Confirmed. We'll start on our part next week.",
};

const bigRunRequest: NetworkRequestRef = {
  id: id("5", 3),
  title: "Design and validate a low-cost Na-ion cathode from Earth-abundant elements",
  requester: castOrgs.henderson,
  status: "active",
  created_at: minutesAgo(38),
};

// Who the big run's needs-human threads wait on: Delta's own admin, and Henderson's
// Maya on Juniper's thread (a requester-side question). The lens names both.
const BIG_RUN_WAITS: Readonly<Record<string, { member: MemberView; minutes: number }>> = {
  delta: { member: networkMembers[3] ?? networkAlex, minutes: 6 },
  juniper: { member: networkMembers[1] ?? networkAlex, minutes: 2 },
};

function bigRunStage(): Stage {
  // Sixteen of the big run's own orgs and three featured ones were contacted, Sable was
  // approved and waits its turn, and the rest of the network was screened out.
  const contacted = [...bigRunOrgs.slice(networkOrgs.length, networkOrgs.length + 16), ...bigRunOrgs.slice(1, 4)];
  const recipients: Record<string, Recipient> = { sable: { participant: "screened", fit: "possible" } };
  const threads: Thread[] = [];
  contacted.forEach(({ org: entry }, index) => {
    const waiting = BIG_RUN_WAITS[entry.slug];
    const participant = waiting === undefined ? (BIG_STATUSES[index % BIG_STATUSES.length] ?? "messaged") : "needs_human";
    recipients[entry.slug] = { participant, fit: index % 3 === 2 ? "possible" : "strong", waiting };
    threads.push({
      id: id("8", index),
      from: castOrgs.henderson,
      to: entry,
      state: STATE_FOR[participant],
      turns: 2 + (index % 7),
      minutes: index % 9,
      latest: BIG_LINES[participant],
      access: [],
    });
  });
  return {
    requester: castOrgs.henderson,
    owner: networkAlex,
    requests: [bigRunRequest],
    requestId: bigRunRequest.id,
    recipients,
    threads,
    orgs: bigRunOrgs,
    worldId: NETWORK_WORLD_ID,
  };
}

export const networkBigRun: NetworkView = view(bigRunStage(), null);

/** Thread ids that animate in the gallery's scripted stream. */
export const typingThreads = {
  messaging: [THREAD_NORTHLAKE],
  question: [THREAD_NORTHLAKE],
  bigRun: networkBigRun.edges.filter((edge) => edge.state === "active").map((edge) => edge.thread_id),
};

/** The focused request's status, for the origin chat's id (a requester-side question). */
function requestStatus(): RequestStatusView {
  return {
    id: REQUEST_ID,
    title: request.title,
    status: request.status,
    brief,
    conversation_id: REQUEST_CONVERSATION_ID,
    created_at: request.created_at,
    screened: null,
    recipients: [],
    version: 1,
  };
}

/**
 * `GET /api/network` at `stage` for the desk world, as the acting member reads it, and
 * the big run for `?world=network` (or a 401 when `bigRun` is false: the session has no
 * network world yet), over the shell's routes.
 */
export function networkRoutes(
  stage: NetworkStage = "needs_human",
  options: { bigRun?: boolean; actingAs?: MemberView } = {},
): FixtureRoutes {
  let bigRunStarted = options.bigRun ?? true;
  const shell = shellRoutes(options.actingAs ?? members.alex);
  // The API's own 401 is a plain FastAPI error, without a problem code (`auth/session.py`).
  const missingWorld = () => json({ detail: "no network world; POST /api/session/start" }, 401);
  const deskSession = async (request: FixtureRequest): Promise<SessionView | null> => {
    const handler = shell["GET /api/session"];
    const base = typeof handler === "function" ? await handler(request) : handler;
    return base === undefined || base instanceof Response ? null : base;
  };
  return {
    ...shell,
    "GET /api/session": async (request) => {
      const base = await deskSession(request);
      if (base === null) return missingWorld();
      if (request.query.get("world") !== "network") return base;
      if (!bigRunStarted) return missingWorld();
      return {
        ...base,
        lens: "network",
        member: networkAlex,
        world: { ...base.world, id: NETWORK_WORLD_ID, mode: "replay" },
      };
    },
    "POST /api/session/start": async (request) => {
      if (request.query.get("world") === "network") bigRunStarted = true;
      const handler = shell["POST /api/session/start"];
      const base = typeof handler === "function" ? await handler(request) : handler;
      return base ?? missingWorld();
    },
    "GET /api/members": ({ query }) => (query.get("world") === "network" ? networkDirectory : memberDirectory),
    "GET /api/network": async (request) => {
      if (request.query.get("world") === "network") return bigRunStarted ? networkBigRun : missingWorld();
      const session = await deskSession(request);
      const current = deskNetwork(stage, session?.member ?? members.alex);
      if (request.query.get("request") === OLDER_REQUEST_ID) {
        return view(
          deskStage({ requests: current.requests, requestId: OLDER_REQUEST_ID, recipients: {}, threads: [] }),
          session?.member ?? members.alex,
        );
      }
      return current;
    },
    "GET /api/requests/{request_id}": ({ params }) =>
      params.request_id === REQUEST_ID ? requestStatus() : problem(404, "not_found", "request not found"),
  };
}
