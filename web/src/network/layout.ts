/**
 * Deterministic canvas layout (UI_REFERENCE.md §8, ARCHITECTURE.md §11.6).
 *
 * - The requester (or, before any request, the acting member's org) is pinned at the
 *   left, vertically centred on the block it talks to.
 * - The focus block fans out to its right: the request's recipients plus any
 *   suggested orgs, ordered by category then name, wrapping into more columns when
 *   there are many (the big run).
 * - Everyone else sits in a band of compact cards under the graph, by category, no
 *   wider than the graph, so framing the whole canvas keeps the cards readable.
 *
 * Positions depend only on who is in which tier, never on status, so a status change
 * repaints a node without moving it.
 */
import type { NetworkNode, NetworkView, WorldLens } from "./types";

export const NODE_WIDTH = 252;
export const NODE_HEIGHT = 86;
export const COMPACT_WIDTH = 204;
export const COMPACT_HEIGHT = 46;

// Wide enough for an edge's label between the requester and the first column
// (`EDGE_LABEL_MAX_WIDTH`), and narrow enough that the story's graph still fits beside
// the side panel at a readable zoom (geometry.ts).
const FOCUS_GAP_X = 148;
/** An edge label's widest: the gap less a margin each side, so it never lands on a card. */
export const EDGE_LABEL_MAX_WIDTH = FOCUS_GAP_X - 16;
const FOCUS_ROW = NODE_HEIGHT + 30;
const FOCUS_COLUMN_STEP = NODE_WIDTH + 72;
const FOCUS_MAX_ROWS = 6;
const REST_GAP = 16;
const REST_GAP_Y = 92;
const REST_ROW = COMPACT_HEIGHT + 12;
const REST_COLUMN_STEP = COMPACT_WIDTH + REST_GAP;
const REST_MIN_COLUMNS = 3;
const LABEL_OFFSET = 30;

// Categories in the order the canvas reads them (§11.6): the work itself first, then
// data, money and the rest.
const KIND_ORDER = [
  "research",
  "characterization",
  "synthesis",
  "computation",
  "fabrication",
  "testing",
  "logistics",
  "data",
  "funder",
  "procurement",
];

export type Tier = "anchor" | "focus" | "rest";

export interface PlacedOrg {
  node: NetworkNode;
  tier: Tier;
  x: number;
  y: number;
  /** Ringed and tagged "Suggested" (the walkthrough's recipients). */
  suggested: boolean;
}

export interface PlacedLabel {
  id: string;
  text: string;
  x: number;
  y: number;
}

export interface NetworkLayout {
  orgs: PlacedOrg[];
  labels: PlacedLabel[];
}

export interface LayoutOptions {
  /** Org ids to ring as suggested (`ringedOrgIds`). */
  suggestedOrgIds?: ReadonlySet<string>;
  /** Pinned at the left when no node is the requester (no request yet). */
  homeOrgId?: string | null;
}

function kindRank(kind: string): number {
  const index = KIND_ORDER.indexOf(kind);
  return index === -1 ? KIND_ORDER.length : index;
}

export function compareByKind(a: NetworkNode, b: NetworkNode): number {
  return (
    kindRank(a.kind) - kindRank(b.kind) ||
    a.kind.localeCompare(b.kind) ||
    a.org.name.localeCompare(b.org.name) ||
    a.org.id.localeCompare(b.org.id)
  );
}

/** A placed org's card size: compact in the band, full everywhere else. */
export function cardSize(tier: Tier): { width: number; height: number } {
  return tier === "rest" ? { width: COMPACT_WIDTH, height: COMPACT_HEIGHT } : { width: NODE_WIDTH, height: NODE_HEIGHT };
}

function blockHeight(rows: number, step: number, itemHeight: number): number {
  return rows === 0 ? 0 : (rows - 1) * step + itemHeight;
}

export function layoutNetwork(nodes: readonly NetworkNode[], options: LayoutOptions = {}): NetworkLayout {
  const suggestedIds = options.suggestedOrgIds ?? new Set<string>();
  const isSuggested = (node: NetworkNode) => suggestedIds.has(node.org.id);

  const requester = nodes.find((node) => node.role === "requester");
  const anchor = requester ?? nodes.find((node) => node.org.id === options.homeOrgId) ?? null;
  const others = nodes.filter((node) => node !== anchor);
  const focus = others.filter((node) => node.role === "recipient" || isSuggested(node)).sort(compareByKind);

  const orgs: PlacedOrg[] = [];
  const labels: PlacedLabel[] = [];

  // Everything is centred on y = 0; the canvas frames it (geometry.ts).
  const focusRows = Math.min(focus.length, FOCUS_MAX_ROWS);
  const focusTop = -blockHeight(focusRows, FOCUS_ROW, NODE_HEIGHT) / 2;
  const focusLeft = anchor === null ? 0 : NODE_WIDTH + FOCUS_GAP_X;
  focus.forEach((node, index) => {
    orgs.push({
      node,
      tier: "focus",
      x: focusLeft + Math.floor(index / FOCUS_MAX_ROWS) * FOCUS_COLUMN_STEP,
      y: focusTop + (index % FOCUS_MAX_ROWS) * FOCUS_ROW,
      suggested: isSuggested(node),
    });
  });

  if (anchor !== null) {
    orgs.unshift({ node: anchor, tier: "anchor", x: 0, y: -NODE_HEIGHT / 2, suggested: false });
  }

  const focusColumns = Math.ceil(focus.length / FOCUS_MAX_ROWS);
  const graphRight =
    focus.length > 0 ? focusLeft + (focusColumns - 1) * FOCUS_COLUMN_STEP + NODE_WIDTH : anchor === null ? 0 : NODE_WIDTH;
  const graphBottom = Math.max(focusTop + blockHeight(focusRows, FOCUS_ROW, NODE_HEIGHT), anchor === null ? 0 : NODE_HEIGHT / 2);

  const rest = others.filter((node) => !focus.includes(node)).sort(compareByKind);
  if (rest.length > 0) {
    // The rest of the network sits in a band under the graph, centred on it.
    const columns = Math.min(rest.length, Math.max(REST_MIN_COLUMNS, Math.floor((graphRight + REST_GAP) / REST_COLUMN_STEP)));
    const bandWidth = columns * REST_COLUMN_STEP - REST_GAP;
    const bandLeft = Math.round((graphRight - bandWidth) / 2);
    const bandTop = focus.length > 0 || anchor !== null ? graphBottom + REST_GAP_Y : 0;
    rest.forEach((node, index) => {
      orgs.push({
        node,
        tier: "rest",
        x: bandLeft + (index % columns) * REST_COLUMN_STEP,
        y: bandTop + Math.floor(index / columns) * REST_ROW,
        suggested: isSuggested(node),
      });
    });
    labels.push({
      id: "label:rest",
      text: bandTop > 0 ? `Elsewhere on the network · ${rest.length}` : `The network · ${rest.length}`,
      x: bandLeft,
      y: bandTop - LABEL_OFFSET,
    });
  }

  return { orgs, labels };
}

/**
 * The orgs the walkthrough rings as suggested (§11.5): the desk world only, never the
 * big run. With a request in focus the API flags them only for viewers who may know
 * whom the requester asked (§11.6); before any request the story's list stands, which
 * the API gives only to the story sender's org. `highlighted` is that list after the
 * demo controls' "highlight suggested recipients" switch, or null while it loads.
 */
export function ringedOrgIds(
  world: WorldLens,
  view: NetworkView | undefined,
  highlighted: ReadonlySet<string> | null,
): ReadonlySet<string> {
  if (world !== "desk" || view === undefined) return new Set();
  if (view.request_id === null) return highlighted ?? new Set();
  return new Set(
    view.nodes
      .filter((node) => node.suggested && (highlighted?.has(node.org.id) ?? true))
      .map((node) => node.org.id),
  );
}
