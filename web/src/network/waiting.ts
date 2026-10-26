/**
 * Who a needs-human node waits on, attributed to the side that holds its thread up
 * (ARCHITECTURE.md §11.5, §11.6), and where answering it happens.
 *
 * A recipient's thread can wait on either side: the responder's team (Northlake asks
 * Helen) or the requester's own member (Henderson asks Alex about the seal). The API
 * puts either on the recipient's node, so the side comes from the member's org.
 *
 * In the desk world `waiting_on` names only the acting member's own org, and only on a
 * thread they can open: who holds up the other side is that org's to know, and what
 * happens on a thread is for those routed or shared into it (`domain/requests.py`). A
 * node can then need a human with no `waiting_on`:
 * - an actor on one side of a thread they can open knows the wait is on the other side.
 *   Nobody is named, and "Act as" suggests an admin of that org instead, since admins
 *   see every thread (§10);
 * - an actor on neither side (Helen looking at Ridgeline's thread with Henderson), or
 *   one who can't open the thread (it may wait on their own teammate), can't tell which
 *   side waits, and gets no action.
 */
import type { MemberView } from "../api/types";
import { firstName } from "../shell/names";
import { formatWait, shortOrgName } from "./format";
import type { NetworkEdge, NetworkNode, OrgRef, WorldLens } from "./types";

export type WaitSide = "requester" | "responder" | "unknown";

export interface Wait {
  side: WaitSide;
  /** The org whose team holds the thread up (for "unknown", the node's). */
  org: OrgRef;
  /** Who it waits on, when the API named them. */
  member: MemberView | null;
  since: string | null;
  /** Who "Act as" switches to: the member, else an admin of `org`. */
  actAs: MemberView | null;
  /** The acting member is the one it waits on. */
  isYou: boolean;
}

export interface WaitContext {
  /** The focused request's requester. */
  requester: OrgRef | null;
  /** The acting member in the world shown. */
  actor: MemberView | null;
  /** The world's members, to place a waiting member in their org. */
  directory: readonly MemberView[];
  /** The recipients (org ids) whose thread the acting member can open. */
  readable: ReadonlySet<string>;
}

/** The recipients whose thread the acting member can open: the API details only those edges. */
export function readableRecipients(edges: readonly NetworkEdge[]): Set<string> {
  return new Set(edges.flatMap((edge) => (edge.turn_count === null ? [] : [edge.to_org_id])));
}

function adminOf(org: OrgRef, directory: readonly MemberView[], actor: MemberView | null): MemberView | null {
  return (
    directory.find((member) => member.org.id === org.id && member.role === "admin" && member.id !== actor?.id) ?? null
  );
}

/** The wait on `node`'s thread, or null when nothing on it waits on a person. */
export function resolveWait(node: NetworkNode, { requester, actor, directory, readable }: WaitContext): Wait | null {
  if (node.role !== "recipient") return null;
  const named = node.waiting_on;

  if (named !== null) {
    const member = directory.find((candidate) => candidate.id === named.member.id) ?? null;
    // A member the directory doesn't know (it is still loading) is read as the
    // responder's, which is what the node's own status says.
    const side: WaitSide = member !== null && member.org.id === requester?.id ? "requester" : "responder";
    const org = side === "requester" && requester !== null ? requester : (member?.org ?? node.org);
    const isYou = actor !== null && named.member.id === actor.id;
    return { side, org, member, since: named.since, actAs: isYou ? null : member, isYou };
  }

  if (node.participant !== "needs_human") return null;
  // Unnamed: the side the actor isn't on, if they're on one (the module docstring).
  const unnamed = { member: null, since: null, isYou: false };
  if (!readable.has(node.org.id)) {
    return { ...unnamed, side: "unknown", org: node.org, actAs: null };
  }
  if (requester !== null && actor?.org.id === requester.id) {
    return { ...unnamed, side: "responder", org: node.org, actAs: adminOf(node.org, directory, actor) };
  }
  if (requester !== null && actor?.org.id === node.org.id) {
    return { ...unnamed, side: "requester", org: requester, actAs: adminOf(requester, directory, actor) };
  }
  return { ...unnamed, side: "unknown", org: node.org, actAs: null };
}

/**
 * The node card's line: "Helen · 4m", "You · 4m", "Alex (Henderson Lab) · 4m" or
 * "Northlake's team"; null when not even the side is known.
 */
export function waitMeta(wait: Wait, now: number): string | null {
  const since = wait.since === null ? "" : ` · ${formatWait(wait.since, now)}`;
  if (wait.isYou) return `You${since}`;
  if (wait.member === null) return wait.side === "unknown" ? null : `${shortOrgName(wait.org.name)}'s team`;
  const who = firstName(wait.member.name);
  return `${wait.side === "requester" ? `${who} (${shortOrgName(wait.org.name)})` : who}${since}`;
}

export type WaitAction =
  /** It waits on the acting member: go and answer it. */
  | { kind: "open"; label: string; to: string }
  /** Switch to the member (or an admin), then open the item. */
  | { kind: "act_as"; member: MemberView; to: string };

export interface WaitPlaces {
  world: WorldLens;
  /** The thread between the requester and the waiting node. */
  threadId: string | null;
  /** The focused request's origin chat: the API names it only to the chat's owner. */
  conversationId: string | null;
}

/**
 * What the panel suggests for a wait. A responder's team answers in Requests. A
 * requester-side question shows inline in the chat that sent the request, but only to
 * that chat's owner, the one member who can open it; anyone else it waits on answers it
 * in Requests too. Only the desk world is the desktop app's, so only it suggests
 * anything.
 */
export function waitAction(wait: Wait, { world, threadId, conversationId }: WaitPlaces): WaitAction | null {
  if (world !== "desk" || wait.side === "unknown") return null;
  const inChat = wait.side === "requester" && wait.isYou && conversationId !== null;
  const to = inChat ? `/chat/${conversationId}` : threadId === null ? "/requests" : `/requests/${threadId}`;
  if (wait.isYou) {
    return { kind: "open", label: inChat ? "Answer in the chat" : "Open in Requests", to };
  }
  return wait.actAs === null ? null : { kind: "act_as", member: wait.actAs, to };
}
