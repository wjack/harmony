import { describe, expect, it } from "vitest";

// The server's entity types (events/schemas.py `EntityType`), read as text.
import serverSchemas from "../../../src/harmony/events/schemas.py?raw";
import { ApiError } from "../api/client";
import type { EventEnvelope, MemberView } from "../api/types";
import { stoppedLine } from "../chat/failureCopy";
import {
  REQUEST_CONVERSATION_ID,
  THREAD_NORTHLAKE,
  deskNetwork,
  networkBefore,
  networkBigRun,
  networkClosed,
  networkDirectory,
  networkMessaging,
  networkNeedsHuman,
  networkOrgs,
  networkQuestion,
  networkStopped,
  THREAD_RIDGELINE,
} from "../fixtures/network";
import { memberDirectory, members, orgs, WORLD_ID } from "../fixtures/shell";
import { isMissingWorld } from "./api";
import { formatWait } from "./format";
import { boundsOf, READABLE_ZOOM, readableFrame } from "./geometry";
import {
  cardSize,
  COMPACT_HEIGHT,
  COMPACT_WIDTH,
  EDGE_LABEL_MAX_WIDTH,
  layoutNetwork,
  NODE_HEIGHT,
  NODE_WIDTH,
  ringedOrgIds,
  type NetworkLayout,
} from "./layout";
import { GRAPH_ENTITY_TYPES, lensSignal, lensStreamUrl, TYPING_TTL_MS } from "./lens";
import {
  closingLine,
  countParticipants,
  countsLine,
  edgeLabel,
  edgeStyle,
  noRequestText,
  threadStatus,
} from "./status";
import type { NetworkNode, NetworkView } from "./types";
import { readableRecipients, resolveWait, waitAction, waitMeta, type WaitContext } from "./waiting";

const SCOPE: EventEnvelope["scope"] = { kind: "world", org_id: null, member_id: null, thread_id: null, session_id: null };

function envelope(event: EventEnvelope["event"]): EventEnvelope {
  return { world_id: WORLD_ID, seq: 12, created_at: null, scope: SCOPE, event };
}

function overlaps(
  a: { x: number; y: number; w: number; h: number },
  b: { x: number; y: number; w: number; h: number },
): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

describe("layoutNetwork", () => {
  it("pins the requester at the left and fans the recipients out to its right", () => {
    const layout = layoutNetwork(networkNeedsHuman.nodes);
    const anchor = layout.orgs.find((placed) => placed.tier === "anchor");
    expect(anchor?.node.org.id).toBe(orgs.henderson.id);
    expect(anchor?.x).toBe(0);
    const focus = layout.orgs.filter((placed) => placed.tier === "focus");
    expect(focus.map((placed) => placed.node.org.id)).toEqual(
      expect.arrayContaining([orgs.northlake.id, orgs.ridgeline.id]),
    );
    for (const placed of focus) expect(placed.x).toBeGreaterThan(NODE_WIDTH);
  });

  it("puts suggested orgs in the focus block, ringed, even before any request", () => {
    const suggested = new Set([orgs.northlake.id, orgs.ridgeline.id, orgs.calder.id]);
    const layout = layoutNetwork(networkBefore.nodes, { suggestedOrgIds: suggested, homeOrgId: orgs.henderson.id });
    expect(layout.orgs.find((placed) => placed.tier === "anchor")?.node.org.id).toBe(orgs.henderson.id);
    const focus = layout.orgs.filter((placed) => placed.tier === "focus");
    expect(new Set(focus.map((placed) => placed.node.org.id))).toEqual(suggested);
    expect(focus.every((placed) => placed.suggested)).toBe(true);
  });

  it("sends everyone else to a labelled band under the graph", () => {
    const layout = layoutNetwork(networkNeedsHuman.nodes);
    const rest = layout.orgs.filter((placed) => placed.tier === "rest");
    const graphBottom = Math.max(
      ...layout.orgs.filter((placed) => placed.tier !== "rest").map((placed) => placed.y + NODE_HEIGHT),
    );
    const outside = networkNeedsHuman.nodes.filter((node) => node.role === "bystander");
    expect(rest.map((placed) => placed.node)).toEqual(expect.arrayContaining(outside));
    expect(rest).toHaveLength(outside.length);
    for (const placed of rest) expect(placed.y).toBeGreaterThan(graphBottom);
    expect(layout.labels[0]?.text).toBe(`Elsewhere on the network · ${String(rest.length)}`);
  });

  it("rings only the orgs it is told to, whatever the nodes' own flag says", () => {
    // The API flags the story's recipients in the big run too; the page passes none there.
    expect(networkBigRun.nodes.some((node) => node.suggested)).toBe(true);
    const layout = layoutNetwork(networkBigRun.nodes);
    expect(layout.orgs.some((placed) => placed.suggested)).toBe(false);
    const vantage = layoutNetwork(networkNeedsHuman.nodes).orgs.find((placed) => placed.node.org.id === orgs.vantage.id);
    expect(vantage).toMatchObject({ tier: "rest", suggested: false });
  });

  it("keeps the band under the graph no wider than the graph, at three columns or more", () => {
    const layout = layoutNetwork(networkNeedsHuman.nodes);
    const right = (tier: string) =>
      Math.max(...layout.orgs.filter((placed) => placed.tier === tier).map((placed) => placed.x + cardSize(placed.tier).width));
    const left = Math.min(...layout.orgs.filter((placed) => placed.tier === "rest").map((placed) => placed.x));
    const columns = new Set(layout.orgs.filter((placed) => placed.tier === "rest").map((placed) => placed.x)).size;
    expect(columns).toBe(3);
    expect(right("rest") - left).toBeLessThanOrEqual(right("focus") + 3 * 16);
  });

  it("places every org exactly once, without overlaps, at network scale", () => {
    const layout = layoutNetwork(networkBigRun.nodes);
    expect(layout.orgs).toHaveLength(networkBigRun.nodes.length);
    const boxes = layout.orgs.map((placed) => ({
      x: placed.x,
      y: placed.y,
      w: placed.tier === "rest" ? COMPACT_WIDTH : NODE_WIDTH,
      h: placed.tier === "rest" ? COMPACT_HEIGHT : NODE_HEIGHT,
    }));
    boxes.forEach((box, index) => {
      for (const other of boxes.slice(index + 1)) expect(overlaps(box, other)).toBe(false);
    });
  });

  it("is deterministic, and positions don't depend on status", () => {
    const a = layoutNetwork(networkMessaging.nodes);
    const b = layoutNetwork(networkNeedsHuman.nodes);
    const position = (layout: typeof a, id: string) => {
      const placed = layout.orgs.find((candidate) => candidate.node.org.id === id);
      return placed === undefined ? null : [placed.x, placed.y];
    };
    for (const { org } of networkOrgs) expect(position(a, org.id)).toEqual(position(b, org.id));
  });
});

function recipient(partial: Partial<NetworkNode>): NetworkNode {
  return {
    org: orgs.calder,
    kind: "computation",
    featured: true,
    role: "recipient",
    participant: null,
    fit: null,
    waiting_on: null,
    suggested: false,
    ...partial,
  };
}

describe("status", () => {
  it("counts recipients by status for the header", () => {
    const counts = countParticipants(networkNeedsHuman.nodes);
    expect(counts).toEqual({ contacted: 2, replied: 1, needsHuman: 1, closed: 0, screened: 1 });
    expect(countsLine(counts)).toEqual(["2 contacted", "1 replied", "1 needs a human", "1 screened"]);
    expect(countsLine(countParticipants(networkClosed.nodes))).toEqual(["2 contacted", "2 closed", "1 screened"]);
  });

  it("counts an approved recipient whose thread hasn't opened as screened, not contacted", () => {
    // The API's shape before a thread opens (test_e4_routes: Calder screened, strong).
    const counts = countParticipants([recipient({ participant: "screened", fit: "strong" })]);
    expect(counts).toMatchObject({ contacted: 0, screened: 1 });
    expect(countParticipants(networkMessaging.nodes)).toMatchObject({ contacted: 1, screened: 2 });
    // Bystanders never count, whatever they carry.
    expect(countParticipants(networkBefore.nodes).contacted).toBe(0);
  });

  it("counts each contacted recipient once, under the status its pill shows", () => {
    const counts = countParticipants(networkBigRun.nodes);
    const { contacted, replied, needsHuman, closed } = counts;
    expect(replied + needsHuman + closed).toBeLessThanOrEqual(contacted);
    const messaged = networkBigRun.nodes.filter((node) => node.participant === "messaged").length;
    expect(messaged + replied + needsHuman + closed).toBe(contacted);
    expect(counts.screened).toBe(1);
  });

  it("labels an edge with the thread's latest line, and an ended one with our summary", () => {
    const [northlake, ridgeline] = networkNeedsHuman.edges;
    if (northlake === undefined || ridgeline === undefined) throw new Error("no edges");
    const parties = [orgs.henderson.name, orgs.northlake.name];
    expect(edgeLabel(northlake, parties, "ok")).toBe(northlake.latest_line);
    expect(edgeLabel(ridgeline, parties, "ok")).toBe(ridgeline.latest_line);
    expect(edgeLabel(northlake, parties, "ok")).not.toBeNull();
    // A thread the viewer can't open: the color says it.
    expect(edgeLabel({ ...northlake, latest_line: null }, parties, "ok")).toBeNull();
    // The line names its sender in full; the edge has room for the short name.
    const attributed = { ...northlake, latest_line: `${orgs.northlake.name}: Yes: Nov 4.` };
    expect(edgeLabel(attributed, parties, "ok")).toBe("Northlake: Yes: Nov 4.");
    expect(edgeLabel(attributed, [orgs.henderson.name], "ok")).toBe(attributed.latest_line);

    const closed = networkClosed.edges.find((edge) => edge.thread_id === THREAD_NORTHLAKE);
    if (closed === undefined) throw new Error("no closed edge");
    expect(edgeLabel(closed, parties, "ok")).toBe(closed.close_summary);
    // With no summary of ours, it says why it ended, naming the org that closed it.
    const unsummarized = { ...closed, close_summary: null };
    expect(edgeLabel(unsummarized, parties, "ok")).toBe("Closed: Northlake closed it");
    expect(edgeLabel(unsummarized, [orgs.henderson.name], "ok")).toBe("Closed");
    expect(edgeLabel({ ...unsummarized, closed_by: null }, parties, "ok")).toBe("Closed");
    expect(closingLine(unsummarized, parties, "ok")).toBe("Northlake closed it");
    expect(closingLine(unsummarized, [orgs.henderson.name], "ok")).toBeNull();
    // Expired is internal: it reads Closed, with its reason.
    const quiet = { ...unsummarized, state: "expired" as const, end_reason: "quiet" as const, closed_by: null };
    expect(edgeLabel(quiet, parties, "ok")).toBe("Closed: both sides went quiet");
    expect(closingLine(quiet, parties, "ok")).toBe("Both sides went quiet");
    expect(edgeLabel({ ...quiet, end_reason: "turn_limit" }, parties, "ok")).toBe("Closed: it reached its turn limit");
    expect(edgeStyle("closed").ended).toBe(true);
    expect(edgeStyle("awaiting_principal").tone).toBe("warning");
  });

  it("says a thread the system stopped is Closed, and why, by the budget's state now", () => {
    const stopped = networkStopped.edges.find((edge) => edge.thread_id === THREAD_RIDGELINE);
    if (stopped === undefined) throw new Error("no stopped edge");
    const parties = [orgs.henderson.name, orgs.ridgeline.name];
    expect(stopped.stop_reason).toBe("demo_budget");
    expect(edgeLabel(stopped, parties, "demo_exhausted")).toBe("Closed: The demo's live-model budget ran out.");
    expect(edgeLabel(stopped, parties, "ok")).toBe("Closed: The demo's live-model budget ran out for a while.");
    expect(closingLine(stopped, parties, "demo_exhausted")).toBe(stoppedLine("demo_budget", "demo_exhausted"));
    // After the other side refused, our side's last turn wrote a summary: it shows.
    const summarized = { ...stopped, stop_reason: "refused" as const, close_summary: "They declined; no quote." };
    expect(edgeLabel(summarized, parties, "ok")).toBe("They declined; no quote.");
    expect(closingLine(summarized, parties, "ok")).toBe("They declined; no quote.");
  });


  it("states a thread in the words its recipient's pill uses", () => {
    expect(threadStatus("awaiting_principal", "needs_human").label).toBe("Needs human");
    expect(threadStatus("active", "replied").label).toBe("Replied");
    expect(threadStatus("active", null).label).toBe("Messaged");
    expect(threadStatus("awaiting_principal", null).label).toBe("Needs human");
    expect(threadStatus("closed", null).label).toBe("Closed");
    expect(threadStatus("expired", null)).toEqual(threadStatus("closed", null));
  });

  it("says there is nothing in flight, not no requests, when only earlier ones are listed", () => {
    expect(noRequestText("desk", false)).toMatch(/^No requests yet\./);
    expect(noRequestText("desk", true)).toBe(
      "Nothing in flight. Ask Claude for something in a new chat, or pick an earlier request above.",
    );
  });

  it("shows a closed thread's summary only to the side that wrote it", () => {
    const summary = (view: NetworkView) => view.edges.find((edge) => edge.thread_id === THREAD_NORTHLAKE)?.close_summary;
    expect(summary(networkClosed)).toMatch(/^Booked CT and compression tests/);
    expect(summary(deskNetwork("closed", members.helen))).toMatch(/^Henderson Frame Lab booked/);
  });
});

describe("format", () => {
  it("says how long something has waited, compactly", () => {
    const now = Date.parse("2026-10-26T10:30:00Z");
    expect(formatWait("2026-10-26T10:29:30Z", now)).toBe("just now");
    expect(formatWait("2026-10-26T10:26:00Z", now)).toBe("4m");
    expect(formatWait("2026-10-26T08:00:00Z", now)).toBe("2h");
    expect(formatWait("2026-10-23T10:30:00Z", now)).toBe("3d");
  });
});

describe("lens", () => {
  it("opens the lens stream from the snapshot's cursor and epoch", () => {
    expect(lensStreamUrl("network", 42, 3)).toBe("/api/events?lens=network&world=network&after=42&epoch=3");
    expect(lensStreamUrl("desk", null, null)).toBe("/api/events?lens=network&world=desk");
  });

  it("refreshes on graph snapshots and pulses on a committed message", () => {
    const message = lensSignal(
      envelope({
        type: "entity.snapshot",
        entity_type: "thread_message",
        entity_id: "m1",
        version: 1,
        data: { thread_id: "t1" },
      }),
    );
    expect(message).toEqual({ refresh: true, typing: null, pulse: "t1" });
    const consult = lensSignal(
      envelope({ type: "entity.snapshot", entity_type: "consult", entity_id: "c1", version: 2, data: {} }),
    );
    expect(consult).toEqual({ refresh: true, typing: null, pulse: null });
  });

  it("marks typing without refetching, and ignores what the graph doesn't show", () => {
    expect(lensSignal(envelope({ type: "stream.typing", org_id: "o", thread_id: "t2", turn_id: "x" }))).toEqual({
      refresh: false,
      typing: "t2",
      pulse: null,
    });
    const note = lensSignal(
      envelope({ type: "entity.snapshot", entity_type: "internal_note", entity_id: "n1", version: 1, data: {} }),
    );
    expect(note.refresh).toBe(false);
  });

  it("refetches on a needs-human cue: the desk can't see the other org's escalation", () => {
    const cue = lensSignal(
      envelope({ type: "demo.needs_human", kind: "needs_human", org_slug: "northlake", member_slug: "helen", entity_id: "e1" }),
    );
    expect(cue.refresh).toBe(true);
  });
});

describe("entity types", () => {
  // The server's list (events/schemas.py EntityType), so a typo or a type that doesn't
  // exist can't silently never match.
  const literal = /EntityType = Literal\[([^\]]*)\]/.exec(serverSchemas)?.[1] ?? "";
  const known = new Set([...literal.matchAll(/"([a-z_]+)"/g)].map((match) => match[1]));

  it("listens only for entity types the server emits", () => {
    expect(known.size).toBeGreaterThan(10);
    for (const type of GRAPH_ENTITY_TYPES) expect(known).toContain(type);
  });
});

function contextFor(view: NetworkView, actor: MemberView, directory: readonly MemberView[]): WaitContext {
  const requester = view.requests.find((request) => request.id === view.request_id)?.requester ?? null;
  return { requester, actor, directory, readable: readableRecipients(view.edges) };
}

function node(view: NetworkView, slug: string): NetworkNode {
  const found = view.nodes.find((candidate) => candidate.org.slug === slug);
  if (found === undefined) throw new Error(`no ${slug} node`);
  return found;
}

/** `view` with `slug`'s thread waiting on `member` instead. */
function withWaitOn(view: NetworkView, slug: string, member: MemberView): NetworkView {
  return {
    ...view,
    nodes: view.nodes.map((candidate) =>
      candidate.org.slug === slug && candidate.waiting_on !== null
        ? { ...candidate, waiting_on: { ...candidate.waiting_on, member: { id: member.id, slug: member.slug, name: member.name } } }
        : candidate,
    ),
  };
}

const deskDirectory = [...memberDirectory.featured, ...memberDirectory.all];
const desk = { world: "desk" as const, threadId: THREAD_NORTHLAKE, conversationId: REQUEST_CONVERSATION_ID };

describe("waiting", () => {
  it("suggests Northlake's admin when the desk can't see who Northlake asked", () => {
    const wait = resolveWait(node(networkNeedsHuman, "northlake"), contextFor(networkNeedsHuman, members.alex, deskDirectory));
    expect(node(networkNeedsHuman, "northlake").waiting_on).toBeNull();
    expect(wait).toMatchObject({ side: "responder", org: orgs.northlake, member: null, isYou: false });
    expect(wait?.actAs?.id).toBe(members.helen.id);
    expect(wait && waitAction(wait, desk)).toEqual({
      kind: "act_as",
      member: members.helen,
      to: `/requests/${THREAD_NORTHLAKE}`,
    });
    expect(wait && waitMeta(wait, Date.now())).toBe("Northlake's team");
  });

  it("puts a requester-side question in the chat, never suggesting acting as yourself", () => {
    const ridgeline = node(networkQuestion, "ridgeline");
    expect(ridgeline.waiting_on?.member.id).toBe(members.alex.id);
    const wait = resolveWait(ridgeline, contextFor(networkQuestion, members.alex, deskDirectory));
    expect(wait).toMatchObject({ side: "requester", org: orgs.henderson, isYou: true, actAs: null });
    expect(wait && waitAction(wait, desk)).toEqual({
      kind: "open",
      label: "Answer in the chat",
      to: `/chat/${REQUEST_CONVERSATION_ID}`,
    });
  });

  it("acts as the requester's member for someone else on the requester side, in Requests", () => {
    // Maya can't open Alex's chat, so the API doesn't name it to her.
    const asMaya = { ...desk, conversationId: null };
    const wait = resolveWait(node(networkQuestion, "ridgeline"), contextFor(networkQuestion, members.maya, deskDirectory));
    expect(wait).toMatchObject({ side: "requester", isYou: false });
    expect(wait && waitAction(wait, asMaya)).toEqual({
      kind: "act_as",
      member: members.alex,
      to: `/requests/${THREAD_NORTHLAKE}`,
    });
    expect(wait && waitMeta(wait, Date.now())).toMatch(/^Alex \(Henderson\)/);
  });

  it("sends a requester-side wait on someone other than the chat's owner to Requests", () => {
    const onMaya = withWaitOn(networkQuestion, "ridgeline", members.maya);
    // Maya looks: the chat that sent the request is Alex's, and the API doesn't name it.
    const hers = resolveWait(node(onMaya, "ridgeline"), contextFor(onMaya, members.maya, deskDirectory));
    expect(hers).toMatchObject({ side: "requester", isYou: true });
    expect(hers && waitAction(hers, { ...desk, conversationId: null })).toEqual({
      kind: "open",
      label: "Open in Requests",
      to: `/requests/${THREAD_NORTHLAKE}`,
    });
    // Alex looks: his chat holds only his own questions, so he acts as Maya in Requests.
    const alexSees = resolveWait(node(onMaya, "ridgeline"), contextFor(onMaya, members.alex, deskDirectory));
    expect(alexSees && waitAction(alexSees, desk)).toEqual({
      kind: "act_as",
      member: members.maya,
      to: `/requests/${THREAD_NORTHLAKE}`,
    });
  });

  it("tells Helen the wait is hers, with no Act as", () => {
    const asHelen = deskNetwork("needs_human", members.helen);
    const wait = resolveWait(node(asHelen, "northlake"), contextFor(asHelen, members.helen, deskDirectory));
    expect(wait).toMatchObject({ side: "responder", isYou: true, actAs: null });
    expect(wait && waitAction(wait, desk)).toMatchObject({ kind: "open", label: "Open in Requests" });
  });

  it("claims no side for a viewer on neither side of the thread", () => {
    // The API never shows Vantage this request; the rule must still not guess.
    const wait = resolveWait(node(networkNeedsHuman, "northlake"), contextFor(networkNeedsHuman, members.jordan, deskDirectory));
    expect(wait).toMatchObject({ side: "unknown", member: null, actAs: null });
    expect(wait && waitAction(wait, desk)).toBeNull();
    expect(wait && waitMeta(wait, Date.now())).toBeNull();
  });

  it("names whoever the lens says, on their own side, and suggests no Act as outside the desk", () => {
    const directory = [...networkDirectory.featured, ...networkDirectory.all];
    const actor = networkDirectory.featured[0];
    if (actor === undefined) throw new Error("no network-world actor");
    const delta = resolveWait(node(networkBigRun, "delta"), contextFor(networkBigRun, actor, directory));
    expect(delta).toMatchObject({ side: "responder", member: { slug: "ines" } });
    const juniper = resolveWait(node(networkBigRun, "juniper"), contextFor(networkBigRun, actor, directory));
    expect(juniper).toMatchObject({ side: "requester", org: orgs.henderson, member: { slug: "maya" } });
    expect(delta && waitAction(delta, { ...desk, world: "network" })).toBeNull();
  });

  it("claims no side on a thread the actor can't open, which may wait on their own teammate", () => {
    const view = networkQuestion;
    expect(readableRecipients(view.edges)).toEqual(new Set([orgs.northlake.id, orgs.ridgeline.id]));
    // As the API sends it to a Henderson member who isn't routed into Ridgeline's thread.
    const ridgeline = { ...node(view, "ridgeline"), waiting_on: null };
    const context = { ...contextFor(view, members.alex, deskDirectory), readable: new Set([orgs.northlake.id]) };
    const wait = resolveWait(ridgeline, context);
    expect(wait).toMatchObject({ side: "unknown", member: null, actAs: null });
    expect(wait && waitMeta(wait, Date.now())).toBeNull();
    expect(wait && waitAction(wait, desk)).toBeNull();
  });

  it("finds no wait on a node that doesn't need anyone", () => {
    const context = contextFor(networkNeedsHuman, members.alex, deskDirectory);
    expect(resolveWait(node(networkNeedsHuman, "ridgeline"), context)).toBeNull();
    expect(resolveWait(node(networkNeedsHuman, "henderson"), context)).toBeNull();
  });
});

describe("the desk's firewall (§11.6)", () => {
  it("shows a recipient's member only their own org and its thread", () => {
    const asHelen = deskNetwork("needs_human", members.helen);
    expect(asHelen.nodes.filter((entry) => entry.role === "recipient").map((entry) => entry.org.slug)).toEqual(["northlake"]);
    expect(node(asHelen, "ridgeline")).toMatchObject({ role: "bystander", participant: null });
    expect(asHelen.nodes.every((entry) => entry.fit === null && !entry.suggested)).toBe(true);
    expect(asHelen.edges.map((edge) => edge.thread_id)).toEqual([THREAD_NORTHLAKE]);
    // Henderson's older request has no thread of hers: it isn't listed.
    expect(asHelen.requests.map((request) => request.id)).toEqual([asHelen.request_id]);
  });

  it("gives the requester's members every recipient, with fit and the story's marks", () => {
    expect(node(networkNeedsHuman, "ridgeline")).toMatchObject({ role: "recipient", fit: "strong", suggested: true });
    expect(node(networkNeedsHuman, "calder")).toMatchObject({
      role: "recipient",
      participant: "screened",
      fit: "no",
      suggested: true,
    });
    expect(node(networkNeedsHuman, "vantage")).toMatchObject({ role: "bystander", participant: null, fit: null });
    expect(networkBefore.nodes.some((entry) => entry.suggested)).toBe(false);
  });
});

describe("ringedOrgIds", () => {
  const story = new Set([orgs.northlake.id, orgs.ridgeline.id, orgs.calder.id]);

  it("rings the API's marks in the desk world, as the demo controls allow", () => {
    expect(ringedOrgIds("desk", networkNeedsHuman, story)).toEqual(story);
    // Highlighting switched off: the story's list comes back empty.
    expect(ringedOrgIds("desk", networkNeedsHuman, new Set())).toEqual(new Set());
    // The story still loading: the API's marks alone.
    expect(ringedOrgIds("desk", networkNeedsHuman, null)).toEqual(story);
  });

  it("rings nothing the API keeps from a recipient's member, nor anything in the big run", () => {
    expect(ringedOrgIds("desk", deskNetwork("needs_human", members.helen), story)).toEqual(new Set());
    expect(ringedOrgIds("network", networkBigRun, story)).toEqual(new Set());
  });

  it("rings the story's picks before any request, when the API marks none", () => {
    expect(ringedOrgIds("desk", networkBefore, story)).toEqual(story);
    expect(ringedOrgIds("desk", networkBefore, null)).toEqual(new Set());
  });
});

/** The resting frame of `view`'s layout in a `width`×`height` pane, and where the graph lands. */
function framed(layout: NetworkLayout, width: number, height: number) {
  const bounds = boundsOf(layout.orgs.map((placed) => ({ x: placed.x, y: placed.y, ...cardSize(placed.tier) })));
  const frame = readableFrame(bounds, width, height);
  return {
    frame,
    left: bounds.x * frame.zoom + frame.x,
    top: bounds.y * frame.zoom + frame.y,
    right: (bounds.x + bounds.width) * frame.zoom + frame.x,
    bottom: (bounds.y + bounds.height) * frame.zoom + frame.y,
  };
}

describe("framing", () => {
  // At 1280×800 the sidebar leaves about 1008×740 for the canvas; the panel takes a third.
  const PANEL_OPEN = { width: 672, height: 740 };
  // A 1100px window: the panel's third leaves the canvas about 555 wide.
  const NARROW = { width: 555, height: 640 };

  it("shows the whole story graph at a readable zoom with the side panel open", () => {
    for (const [pane, least] of [
      [PANEL_OPEN, 0.85],
      [NARROW, READABLE_ZOOM],
    ] as const) {
      for (const view of [networkMessaging, networkNeedsHuman, networkClosed]) {
        const { frame, left, top, right, bottom } = framed(layoutNetwork(view.nodes), pane.width, pane.height);
        expect(frame.zoom).toBeGreaterThanOrEqual(least);
        expect(left).toBeGreaterThanOrEqual(0);
        expect(top).toBeGreaterThanOrEqual(0);
        expect(right).toBeLessThanOrEqual(pane.width);
        expect(bottom).toBeLessThanOrEqual(pane.height);
      }
    }
  });

  it("centres a graph that fits, never zooming past 110%", () => {
    const { frame, left, right } = framed(layoutNetwork(networkClosed.nodes), 1600, 1200);
    expect(frame.zoom).toBe(1.1);
    expect(left - 56).toBeCloseTo(1600 - 24 - right, 5);
  });

  it("holds a readable zoom for the big run, pinned at the requester's side", () => {
    const { frame, left, right } = framed(layoutNetwork(networkBigRun.nodes), PANEL_OPEN.width, PANEL_OPEN.height);
    expect(frame.zoom).toBe(READABLE_ZOOM);
    expect(left).toBe(56);
    expect(right).toBeGreaterThan(PANEL_OPEN.width);
    // An axis that overflows starts at the padding rather than centring off-screen.
    expect(framed(layoutNetwork(networkBigRun.nodes), 672, 400).top).toBe(32);
  });
});

describe("edge labels", () => {
  it("fit the gap between the requester and the first column, never reaching a card", () => {
    const layout = layoutNetwork(networkNeedsHuman.nodes);
    const anchor = layout.orgs.find((placed) => placed.tier === "anchor");
    const firstColumn = Math.min(...layout.orgs.filter((placed) => placed.tier === "focus").map((placed) => placed.x));
    if (anchor === undefined) throw new Error("no requester");
    const gap = firstColumn - (anchor.x + NODE_WIDTH);
    // Centred in the gap, with room each side.
    expect(gap - EDGE_LABEL_MAX_WIDTH).toBeGreaterThanOrEqual(16);
  });
});

describe("isMissingWorld", () => {
  it("reads the API's own 401 as a world the session hasn't started", () => {
    expect(isMissingWorld(new ApiError(401, "401 no network world", null, "no network world"))).toBe(true);
  });

  it("never reads the passcode gate's 401 as a missing world", () => {
    const gate = { detail: "Enter the passcode.", code: "passcode_required" };
    expect(isMissingWorld(new ApiError(401, "401 Enter the passcode.", gate, gate.detail))).toBe(false);
  });

  it("is false for anything else", () => {
    expect(isMissingWorld(new ApiError(404, "404 not found"))).toBe(false);
    expect(isMissingWorld(new Error("offline"))).toBe(false);
  });
});

describe("typing", () => {
  it("keeps an edge moving past one late heartbeat (they come every 5 s)", () => {
    expect(TYPING_TTL_MS).toBe(8_000);
  });
});
