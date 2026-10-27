import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Edge, type EdgeProps } from "@xyflow/react";
import { memo } from "react";

import { cx } from "../shell/ui";
import { EDGE_LABEL_MAX_WIDTH } from "./layout";
import { TONE_STROKE, edgeStyle } from "./status";
import type { NetworkEdge } from "./types";

export interface ThreadEdgeData extends Record<string, unknown> {
  edge: NetworkEdge;
  /** A turn is streaming on this thread: the dash moves. */
  inFlight: boolean;
  /** Bumps on each committed message; a dot travels the edge. */
  pulse: number;
  /** What the edge says (`edgeLabel`), where it has open canvas to land on; else null. */
  label: string | null;
}

export type ThreadFlowEdge = Edge<ThreadEdgeData, "thread">;

const LABEL_TONE = {
  neutral: "text-secondary",
  outline: "text-secondary",
  info: "text-info",
  success: "text-success",
  warning: "text-warning",
  muted: "text-faint",
} as const;

/** A thread (UI_REFERENCE.md §8): colored by state, dashed once ended, moving while a turn runs. */
export const ThreadEdge = memo(function ThreadEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  selected,
}: EdgeProps<ThreadFlowEdge>) {
  // The label sits at the curve's middle, which is the middle of the gap between the cards.
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  if (data === undefined) return <BaseEdge id={id} path={path} />;

  const style = edgeStyle(data.edge.state);
  const stroke = TONE_STROKE[style.tone];
  const { label } = data;

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        className={cx("harmony-thread-edge", data.inFlight && "harmony-thread-edge--flowing")}
        style={{
          stroke,
          strokeWidth: selected ? 2.5 : 1.5,
          strokeDasharray: style.ended ? "3 5" : data.inFlight ? "6 5" : undefined,
          opacity: style.ended ? 0.7 : 1,
        }}
      />
      {data.pulse > 0 && (
        // Keyed on the counter, so each new message restarts the animation.
        <circle key={data.pulse} r={3.5} fill={stroke} opacity={0}>
          <animateMotion dur="1.2s" repeatCount="1" path={path} fill="freeze" />
          <animate attributeName="opacity" values="0;1;1;0" keyTimes="0;0.1;0.8;1" dur="1.2s" fill="freeze" />
        </circle>
      )}
      {label !== null && (
        <EdgeLabelRenderer>
          <div
            title={label}
            data-edge-label
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              maxWidth: EDGE_LABEL_MAX_WIDTH,
            }}
            // Two lines before it's cut, so the sender's name and some of what they said
            // both show; the radius keeps one line a pill.
            className={cx(
              "nodrag nopan pointer-events-auto absolute line-clamp-2 rounded-[11px] border border-border bg-surface px-2 py-0.5 text-[11px] leading-4 font-medium break-words shadow-card",
              LABEL_TONE[style.tone],
            )}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
});
