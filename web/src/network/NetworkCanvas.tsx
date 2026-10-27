import "./network.css";

import {
  Background,
  BackgroundVariant,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useStore,
  type NodeChange,
  type XYPosition,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { BudgetState } from "../app/budget";
import { CanvasControls } from "./CanvasControls";
import { boundsOf, readableFrame, type Rect } from "./geometry";
import { cardSize, layoutNetwork } from "./layout";
import type { LensActivity } from "./lens";
import { LabelNode, OrgNode, type LabelFlowNode, type OrgFlowNode } from "./OrgNode";
import { edgeLabel, edgeStyle } from "./status";
import { ThreadEdge, type ThreadFlowEdge } from "./ThreadEdge";
import type { NetworkView } from "./types";
import type { Wait } from "./waiting";

const NODE_TYPES = { org: OrgNode, label: LabelNode };
const EDGE_TYPES = { thread: ThreadEdge };
const NO_ACTIVITY: LensActivity = { inFlight: new Set(), pulses: new Map() };
const NO_WAITS: ReadonlyMap<string, Wait> = new Map();

type FlowNode = OrgFlowNode | LabelFlowNode;

interface NodeOverride {
  /** Where the visitor dragged it. */
  position?: XYPosition;
  measured?: { width: number; height: number };
}

export interface NetworkCanvasProps {
  view: NetworkView;
  /** Ringed and tagged "Suggested" (the walkthrough's picks, desk world only). */
  suggestedOrgIds?: ReadonlySet<string>;
  /** Per org id, who its thread waits on (waiting.ts). */
  waits?: ReadonlyMap<string, Wait>;
  /** The acting member's org: tagged "You", and pinned left before any request. */
  homeOrgId?: string | null;
  activity?: LensActivity;
  selectedOrgId: string | null;
  onSelectOrg: (orgId: string | null) => void;
  /** The clock waiting times are measured against. */
  now: number;
  /** The live-model budget's state now, which a stopped thread's line reads by. */
  budget: BudgetState;
}

function Canvas({
  view,
  suggestedOrgIds,
  waits = NO_WAITS,
  homeOrgId = null,
  activity = NO_ACTIVITY,
  selectedOrgId,
  onSelectOrg,
  now,
  budget,
}: NetworkCanvasProps) {
  const flow = useReactFlow<FlowNode, ThreadFlowEdge>();
  const [overrides, setOverrides] = useState<ReadonlyMap<string, NodeOverride>>(new Map());

  const layout = useMemo(
    () => layoutNetwork(view.nodes, { suggestedOrgIds, homeOrgId }),
    [view.nodes, suggestedOrgIds, homeOrgId],
  );

  // Frame the graph where the cards sit now (dragged ones included) at a readable zoom.
  const width = useStore((state) => state.width);
  const height = useStore((state) => state.height);
  const frameGraph = useCallback(
    (duration: number) => {
      const rects = flow.getNodes().map(
        (node): Rect => ({
          ...node.position,
          ...(node.type === "org" ? cardSize(node.data.placed.tier) : { width: 0, height: 16 }),
        }),
      );
      void flow.setViewport(readableFrame(boundsOf(rects), width, height), { duration });
    },
    [flow, width, height],
  );

  // Reframe when the canvas resizes (the side panel opening) or a different set of
  // orgs is shown (another request or world), so nothing ends up under the panel. The
  // first frame lands at once, so the canvas never opens on React Flow's default.
  const framed = useRef(false);
  const orgSet = layout.orgs.map((placed) => placed.node.org.id).join(",");
  useEffect(() => {
    if (width === 0 || height === 0) return;
    const frame = requestAnimationFrame(() => {
      frameGraph(framed.current ? 200 : 0);
      framed.current = true;
    });
    return () => {
      cancelAnimationFrame(frame);
    };
  }, [frameGraph, width, height, orgSet]);

  const built = useMemo((): FlowNode[] => {
    const threadsByOrg = new Map<string, number>();
    for (const edge of view.edges) {
      threadsByOrg.set(edge.from_org_id, (threadsByOrg.get(edge.from_org_id) ?? 0) + 1);
    }
    const orgNodes = layout.orgs.map(
      (placed): OrgFlowNode => ({
        id: placed.node.org.id,
        type: "org",
        position: { x: placed.x, y: placed.y },
        data: {
          placed,
          isHome: placed.node.org.id === homeOrgId,
          threadCount: threadsByOrg.get(placed.node.org.id) ?? 0,
          wait: waits.get(placed.node.org.id) ?? null,
          now,
        },
        ariaLabel: placed.node.org.name,
      }),
    );
    const labelNodes = layout.labels.map(
      (label): LabelFlowNode => ({
        id: label.id,
        type: "label",
        position: { x: label.x, y: label.y },
        data: { text: label.text },
        draggable: false,
        selectable: false,
        focusable: false,
      }),
    );
    return [...orgNodes, ...labelNodes];
  }, [layout, view.edges, homeOrgId, waits, now]);

  const nodes = useMemo(
    () =>
      built.map((node): FlowNode => {
        const override = overrides.get(node.id);
        return {
          ...node,
          ...(override?.position !== undefined ? { position: override.position } : {}),
          ...(override?.measured !== undefined ? { measured: override.measured } : {}),
          selected: node.id === selectedOrgId,
        };
      }),
    [built, overrides, selectedOrgId],
  );

  const edges = useMemo(() => {
    // A label sits in the gap between the requester and the first column, so it only
    // has open canvas to land on when the edge ends there; further columns show their
    // state on the card.
    const anchorId = layout.orgs.find((placed) => placed.tier === "anchor")?.node.org.id ?? null;
    const xOf = new Map(layout.orgs.map((placed) => [placed.node.org.id, placed.x]));
    const nameOf = new Map(layout.orgs.map((placed) => [placed.node.org.id, placed.node.org.name]));
    const focusX = layout.orgs.filter((placed) => placed.tier === "focus").map((placed) => placed.x);
    const firstColumnX = focusX.length > 0 ? Math.min(...focusX) : null;
    const labelFits = (from: string, to: string) => from !== anchorId || xOf.get(to) === firstColumnX;
    return view.edges.map(
      (edge): ThreadFlowEdge => ({
        id: edge.thread_id,
        type: "thread",
        source: edge.from_org_id,
        target: edge.to_org_id,
        selected: edge.to_org_id === selectedOrgId || edge.from_org_id === selectedOrgId,
        data: {
          edge,
          inFlight: activity.inFlight.has(edge.thread_id) && !edgeStyle(edge.state).ended,
          pulse: activity.pulses.get(edge.thread_id) ?? 0,
          label: labelFits(edge.from_org_id, edge.to_org_id)
            ? edgeLabel(edge, [edge.from_org_id, edge.to_org_id].flatMap((orgId) => nameOf.get(orgId) ?? []), budget)
            : null,
        },
      }),
    );
  }, [view.edges, layout, activity, selectedOrgId, budget]);

  // Positions and measured sizes are ours to keep; selection is driven by the page.
  const onNodesChange = useCallback((changes: NodeChange<FlowNode>[]) => {
    setOverrides((current) => {
      let next: Map<string, NodeOverride> | null = null;
      for (const change of changes) {
        if (change.type === "position" && change.position !== undefined) {
          next ??= new Map(current);
          next.set(change.id, { ...next.get(change.id), position: change.position });
        } else if (change.type === "dimensions" && change.dimensions !== undefined) {
          next ??= new Map(current);
          next.set(change.id, { ...next.get(change.id), measured: change.dimensions });
        }
      }
      return next ?? current;
    });
  }, []);

  const autoLayout = useCallback(() => {
    setOverrides((current) => {
      const next = new Map<string, NodeOverride>();
      for (const [id, override] of current) {
        if (override.measured !== undefined) next.set(id, { measured: override.measured });
      }
      return next;
    });
    requestAnimationFrame(() => {
      frameGraph(300);
    });
  }, [frameGraph]);

  return (
    <ReactFlow<FlowNode, ThreadFlowEdge>
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      edgeTypes={EDGE_TYPES}
      onNodesChange={onNodesChange}
      onNodeClick={(_, node) => {
        if (node.type === "org") onSelectOrg(node.id);
      }}
      onEdgeClick={(_, edge) => {
        onSelectOrg(edge.target);
      }}
      onPaneClick={() => {
        onSelectOrg(null);
      }}
      nodesConnectable={false}
      edgesFocusable={false}
      minZoom={0.25}
      maxZoom={1.75}
      proOptions={{ hideAttribution: true }}
      aria-label="Network graph"
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1.4} />
      <CanvasControls onAutoLayout={autoLayout} />
    </ReactFlow>
  );
}

/** The React Flow canvas: org nodes, thread edges, dotted background, controls. */
export function NetworkCanvas(props: NetworkCanvasProps) {
  return (
    <div className="harmony-network size-full">
      <ReactFlowProvider>
        <Canvas {...props} />
      </ReactFlowProvider>
    </div>
  );
}
