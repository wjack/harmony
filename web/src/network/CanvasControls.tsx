import { Panel, useReactFlow } from "@xyflow/react";
import { LayoutGrid, Maximize, Minus, Plus, type LucideIcon } from "lucide-react";

import { Tooltip } from "../shell/ui";
import { fitOptions } from "./geometry";

const ICON = { size: 15, strokeWidth: 1.75 } as const;

interface ControlProps {
  label: string;
  Icon: LucideIcon;
  onClick: () => void;
}

function Control({ label, Icon, onClick }: ControlProps) {
  return (
    <Tooltip content={label} side="right">
      <button
        type="button"
        aria-label={label}
        onClick={onClick}
        className="flex size-8 items-center justify-center text-secondary transition-colors duration-100 hover:bg-hover hover:text-ink"
      >
        <Icon {...ICON} />
      </button>
    </Tooltip>
  );
}

const GROUP = "flex flex-col overflow-hidden rounded-control border border-border bg-surface shadow-card";

interface CanvasControlsProps {
  /** Put every node back where the layout wants it, then frame them. */
  onAutoLayout: () => void;
  /** Space kept clear at the top for a note floating over the canvas. */
  topInset: number;
}

/** Zoom, fit and auto-layout, stacked at the bottom left as on Railway's canvas. */
export function CanvasControls({ onAutoLayout, topInset }: CanvasControlsProps) {
  const flow = useReactFlow();
  return (
    <Panel position="bottom-left" style={{ margin: 16 }} className="flex flex-col gap-2">
      <div className={GROUP}>
        <Control label="Zoom in" Icon={Plus} onClick={() => void flow.zoomIn({ duration: 160 })} />
        <div className="h-px bg-border" />
        <Control label="Zoom out" Icon={Minus} onClick={() => void flow.zoomOut({ duration: 160 })} />
        <div className="h-px bg-border" />
        <Control label="Fit view" Icon={Maximize} onClick={() => void flow.fitView({ ...fitOptions(topInset), duration: 240 })} />
      </div>
      <div className={GROUP}>
        <Control label="Auto-layout" Icon={LayoutGrid} onClick={onAutoLayout} />
      </div>
    </Panel>
  );
}
