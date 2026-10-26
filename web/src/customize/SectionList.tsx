import { ChevronDown } from "lucide-react";
import { useState, type ReactNode } from "react";

import { cx } from "../shell/ui";
import { SECTION_GROUPS, type SectionGroup } from "./sections";
import type { HarmonySection, OrgRef } from "./types";

function Group({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="mt-1 first:mt-0">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((value) => !value);
        }}
        className="flex h-7 w-full items-center gap-1 rounded-control px-2 text-xs text-muted hover:text-secondary"
      >
        <ChevronDown
          size={12}
          strokeWidth={2}
          className={cx("shrink-0 transition-transform duration-150", !open && "-rotate-90")}
          aria-hidden
        />
        {label}
      </button>
      {open && <ul className="flex flex-col gap-px">{children}</ul>}
    </div>
  );
}

interface SectionListProps {
  org: OrgRef;
  section: HarmonySection;
  onSelect: (section: HarmonySection) => void;
  /** The trailing hint on each row ("Auto", "12"). */
  meta: Partial<Record<HarmonySection, string>>;
  /** Panes with unsaved edits. */
  unsaved: ReadonlySet<HarmonySection>;
  editable: boolean;
}

/** The list column: the org's settings panes, grouped like a connector list (UI_REFERENCE.md §6). */
export function SectionList({ org, section, onSelect, meta, unsaved, editable }: SectionListProps) {
  const renderGroup = (group: SectionGroup) => (
    <Group key={group.label} label={group.label}>
      {group.sections.map(({ id, label, Icon }) => {
        const selected = id === section;
        return (
          <li key={id}>
            <button
              type="button"
              aria-current={selected ? "page" : undefined}
              onClick={() => {
                onSelect(id);
              }}
              className={cx(
                "flex h-8 w-full items-center gap-2.5 rounded-control px-2 text-sm leading-5 text-ink transition-colors duration-100",
                selected ? "bg-hover font-medium" : "hover:bg-hover",
              )}
            >
              <Icon size={16} strokeWidth={1.5} className="shrink-0 text-secondary" aria-hidden />
              <span className="min-w-0 flex-1 truncate text-left">{label}</span>
              {unsaved.has(id) && (
                <span className="size-1.5 shrink-0 rounded-full bg-accent" aria-label="Unsaved changes" />
              )}
              {meta[id] !== undefined && (
                <span className="shrink-0 text-xs font-normal text-muted tabular-nums">{meta[id]}</span>
              )}
            </button>
          </li>
        );
      })}
    </Group>
  );

  return (
    <div className="flex w-[300px] shrink-0 flex-col border-r border-border">
      <div className="flex h-12 shrink-0 items-center gap-2 px-4">
        <h1 className="min-w-0 flex-1 text-[15px] leading-5 font-semibold text-ink">Harmony</h1>
        <span className="text-xs text-muted">{editable ? "Admin" : "View only"}</span>
      </div>
      <p className="-mt-2 truncate px-4 pb-2 text-[13px] leading-5 text-muted">{org.name}</p>
      <nav aria-label="Harmony settings" className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto px-2 pb-4">
        {SECTION_GROUPS.map(renderGroup)}
      </nav>
    </div>
  );
}
