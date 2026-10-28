import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { memo } from "react";

import { OrgAvatar, cx } from "../shell/ui";
import { kindLabel } from "./format";
import { COMPACT_HEIGHT, COMPACT_WIDTH, NODE_HEIGHT, NODE_WIDTH, type PlacedOrg } from "./layout";
import { StatusPill } from "./StatusPill";
import type { Fit } from "./types";
import { waitMeta, type Wait } from "./waiting";

export interface OrgNodeData extends Record<string, unknown> {
  placed: PlacedOrg;
  /** The acting member's own org. */
  isHome: boolean;
  /** Threads this org is on in the focused request (shown on the requester). */
  threadCount: number;
  /** Who this org's thread waits on, attributed to its side. */
  wait: Wait | null;
  /** The clock the waiting time is measured against. */
  now: number;
}

export type OrgFlowNode = Node<OrgNodeData, "org">;

const FIT_LABELS: Record<Fit, string> = {
  strong: "Strong fit",
  possible: "Possible fit",
  no: "Not a fit",
  unknown: "Fit unknown",
};

// The walkthrough's orange ring (UI_REFERENCE.md §8): a soft halo outside the border.
const SUGGESTED_RING = {
  boxShadow: "0 0 0 4px color-mix(in srgb, var(--accent) 22%, transparent), var(--elevation-card)",
} as const;

// Edges attach at the card's sides; the handles themselves stay invisible.
const HANDLE = "!size-1.5 !min-h-0 !min-w-0 !border-0 !bg-transparent";

function Handles() {
  return (
    <>
      <Handle type="target" position={Position.Left} className={HANDLE} isConnectable={false} />
      <Handle type="source" position={Position.Right} className={HANDLE} isConnectable={false} />
    </>
  );
}

// Each box is two of its name's lines tall (18px, 16px).
const TWO_LINES = {
  card: { box: "max-h-9", name: "text-ui-sm font-semibold", meta: "text-ui-xs" },
  compact: { box: "max-h-8", name: "text-ui-xs font-medium", meta: "text-[11px] leading-4" },
} as const;

/**
 * A card's two lines: the org's name, then `meta`. A name too long for one line takes
 * the meta's line instead of being cut, and only a name longer than two lines ends in
 * "…", so a name that fits never looks cut short.
 */
function TwoLines({ name, meta, size }: { name: string; meta: string; size: keyof typeof TWO_LINES }) {
  const style = TWO_LINES[size];
  return (
    <div title={name} className={cx("min-w-0 flex-1 overflow-hidden", style.box)}>
      <div data-org-name className={cx("line-clamp-2 break-words text-ink", style.name)}>
        {name}
      </div>
      <div className={cx("truncate text-muted", style.meta)}>{meta}</div>
    </div>
  );
}

/** The requester or a recipient (or a suggested org not contacted): Railway's service card in Claude's tokens. */
function FullCard({ data, selected }: { data: OrgNodeData; selected: boolean }) {
  const { node, tier, suggested } = data.placed;
  const status = node.participant;
  const anchor = tier === "anchor";
  const waiting = data.wait === null ? null : waitMeta(data.wait, data.now);
  const meta = waiting ?? (node.fit !== null && status === "screened" ? FIT_LABELS[node.fit] : null);

  return (
    <div
      data-suggested={suggested || undefined}
      style={{ width: NODE_WIDTH, height: NODE_HEIGHT, ...(suggested ? SUGGESTED_RING : {}) }}
      className={cx(
        "relative flex flex-col justify-between rounded-[12px] border bg-surface px-3 pt-3 pb-2.5 text-left shadow-card transition-[box-shadow,border-color] duration-150",
        selected ? "border-ink shadow-menu" : "border-border hover:border-border-strong hover:shadow-menu",
        status === "needs_human" && !selected && "border-[color-mix(in_srgb,var(--warning)_45%,var(--border))]",
        suggested && !selected && "border-accent",
      )}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        <OrgAvatar name={node.org.name} seed={node.org.slug} size={28} />
        <TwoLines name={node.org.name} meta={`${kindLabel(node.kind)}${data.isHome ? " · You" : ""}`} size="card" />
      </div>
      <div className="flex min-w-0 items-center justify-between gap-2">
        {anchor ? (
          <span className="inline-flex h-[22px] items-center rounded-full bg-hover px-2 text-xs leading-none font-medium text-secondary">
            {node.role === "requester" ? "Requester" : "Your organization"}
          </span>
        ) : status === null ? (
          <span className="text-xs text-muted">Not contacted</span>
        ) : (
          <StatusPill status={status} />
        )}
        {anchor ? (
          data.threadCount > 0 && (
            <span className="truncate text-xs text-muted">
              {data.threadCount === 1 ? "1 thread" : `${data.threadCount} threads`}
            </span>
          )
        ) : (
          meta !== null && (
            <span
              className={cx(
                "truncate text-xs tabular-nums",
                waiting !== null ? "font-medium text-warning" : "text-muted",
              )}
            >
              {meta}
            </span>
          )
        )}
      </div>
      <Handles />
    </div>
  );
}

/** Everyone else on the network: a compact card, still selectable. */
function CompactCard({ data, selected }: { data: OrgNodeData; selected: boolean }) {
  const { node, suggested } = data.placed;
  const status = node.participant;
  const meta = status === "screened" && node.fit !== null ? FIT_LABELS[node.fit] : kindLabel(node.kind);
  return (
    <div
      style={{ width: COMPACT_WIDTH, height: COMPACT_HEIGHT, ...(suggested ? SUGGESTED_RING : {}) }}
      className={cx(
        "relative flex items-center gap-2 rounded-control border bg-surface px-2.5 text-left transition-[box-shadow,border-color] duration-150",
        selected ? "border-ink shadow-menu" : suggested ? "border-accent" : "border-border hover:border-border-strong hover:shadow-card",
      )}
    >
      <OrgAvatar name={node.org.name} seed={node.org.slug} size={22} />
      <TwoLines name={node.org.name} meta={`${meta}${data.isHome ? " · You" : ""}`} size="compact" />
      {status !== null && status !== "screened" && <StatusPill status={status} size="sm" />}
      <Handles />
    </div>
  );
}

export const OrgNode = memo(function OrgNode({ data, selected }: NodeProps<OrgFlowNode>) {
  return data.placed.tier === "rest" ? (
    <CompactCard data={data} selected={selected} />
  ) : (
    <FullCard data={data} selected={selected} />
  );
});

export interface LabelNodeData extends Record<string, unknown> {
  text: string;
}

export type LabelFlowNode = Node<LabelNodeData, "label">;

/** A muted section label on the canvas ("Elsewhere on the network · 11"). */
export const LabelNode = memo(function LabelNode({ data }: NodeProps<LabelFlowNode>) {
  return <div className="text-xs font-medium whitespace-nowrap text-muted select-none">{data.text}</div>;
});
