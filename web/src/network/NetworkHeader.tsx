import { Check, ChevronDown, Eye, UserRound } from "lucide-react";

import { Menu, MenuContent, MenuItem, MenuLabel, MenuTrigger, OrgAvatar, Tooltip, cx } from "../shell/ui";
import { useUnavailable } from "../shell/unavailable";
import { formatAgo } from "./format";
import type { ParticipantCounts } from "./status";
import { countsLine } from "./status";
import type { NetworkRequestRef, RequestStatus, WorldLens } from "./types";

const REQUEST_STATUS: Record<RequestStatus, string> = {
  active: "Active",
  closed: "Closed",
};

// The big run (one request fanned out to the whole network) is a later phase
// (ARCHITECTURE.md §15, P3); until then its option opens the not-available dialog
// instead of a world that has nothing of its own to show.
const WORLDS: readonly { value: WorldLens; label: string; hint: string; available: boolean }[] = [
  { value: "desk", label: "Your desk", hint: "Your own requests and the orgs they reached", available: true },
  {
    value: "network",
    label: "Big run",
    hint: "Coming later: one request fanned out to the whole network",
    available: false,
  },
];

interface RequestPickerProps {
  requests: readonly NetworkRequestRef[];
  requestId: string | null;
  onChange: (requestId: string) => void;
  now: number;
}

/** Which network request the canvas shows, styled like the chat title's dropdown. */
function RequestPicker({ requests, requestId, onChange, now }: RequestPickerProps) {
  const current = requests.find((request) => request.id === requestId) ?? null;
  if (requests.length === 0) {
    return <span className="truncate text-sm font-medium text-muted">No requests yet</span>;
  }
  return (
    <Menu>
      <MenuTrigger asChild>
        <button
          type="button"
          className="-ml-2 flex h-8 max-w-[320px] min-w-[120px] shrink-[4] items-center gap-1.5 rounded-control px-2 text-sm font-medium text-ink transition-colors hover:bg-hover data-[state=open]:bg-hover"
        >
          <span className="truncate">{current?.title ?? "Choose a Harmony Network Request"}</span>
          <ChevronDown size={15} strokeWidth={1.75} className="shrink-0 text-muted" aria-hidden />
        </button>
      </MenuTrigger>
      <MenuContent align="start" className="w-[380px]">
        <MenuLabel>Harmony Network Requests</MenuLabel>
        {requests.map((request) => (
          <MenuItem
            key={request.id}
            className="py-2"
            icon={<OrgAvatar name={request.requester.name} seed={request.requester.slug} size={20} />}
            onSelect={() => {
              onChange(request.id);
            }}
          >
            <div className="flex items-center gap-2">
              <div className="min-w-0 flex-1">
                <div className="truncate">{request.title}</div>
                <div className="truncate text-xs text-muted">
                  {request.requester.name} · {REQUEST_STATUS[request.status]} · {formatAgo(request.created_at, now)}
                </div>
              </div>
              {request.id === requestId && <Check size={16} strokeWidth={1.75} className="shrink-0" aria-hidden />}
            </div>
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );
}

interface WorldSwitchProps {
  world: WorldLens;
  onChange: (world: WorldLens) => void;
}

/** Desk world or the big run (ARCHITECTURE.md §11.6), as a two-way segmented control. */
export function WorldSwitch({ world, onChange }: WorldSwitchProps) {
  const unavailable = useUnavailable();
  return (
    <div role="radiogroup" aria-label="World" className="inline-flex h-8 items-center rounded-control bg-hover p-0.5">
      {WORLDS.map((option) => {
        const selected = option.value === world;
        // Clickable, like every out-of-scope control: it says so rather than doing nothing.
        const later = !option.available && !selected;
        return (
          <Tooltip key={option.value} content={option.hint} side="bottom">
            <button
              type="button"
              role="radio"
              aria-checked={selected}
              aria-description={later ? option.hint : undefined}
              onClick={() => {
                if (later) unavailable(`The ${option.label.toLowerCase()}`);
                else onChange(option.value);
              }}
              className={cx(
                "h-7 rounded-[6px] px-3 text-[13px] font-medium transition-colors duration-100",
                selected
                  ? "bg-surface text-ink shadow-card"
                  : later
                    ? "text-faint hover:text-muted"
                    : "text-muted hover:text-ink",
              )}
            >
              {option.label}
            </button>
          </Tooltip>
        );
      })}
    </div>
  );
}

function WorldBadge({ mode }: { mode: "live" | "replay" }) {
  return (
    <span className="inline-flex h-6 items-center gap-1.5 rounded-full border border-border bg-surface px-2 text-xs font-medium text-secondary">
      <span
        aria-hidden
        className={cx("size-1.5 rounded-full", mode === "live" ? "animate-pulse-dot bg-success" : "bg-faint")}
      />
      {mode === "live" ? "Live" : "Replay"}
    </span>
  );
}

// The desk shows what the acting member may see (§11.3); the big run is the demo lens
// over both sides of every thread (§8), and says so.
const SCOPES: Record<WorldLens, { label: string; hint: string; Icon: typeof Eye }> = {
  desk: {
    label: "Your side",
    hint: "Your desk shows what you can see. Another org's internal details, like who it asked, stay theirs.",
    Icon: UserRound,
  },
  network: {
    label: "Demo lens",
    hint: "A demo lens: it sees both sides of every thread. Members only ever see their own side.",
    Icon: Eye,
  },
};

function ScopeBadge({ world }: { world: WorldLens }) {
  const { label, hint, Icon } = SCOPES[world];
  return (
    <Tooltip side="bottom" content={hint}>
      <span className="inline-flex h-6 cursor-default items-center gap-1.5 rounded-full px-1.5 text-xs font-medium text-muted">
        <Icon size={14} strokeWidth={1.75} aria-label={label} />
        <span className="hidden 2xl:inline">{label}</span>
      </span>
    </Tooltip>
  );
}

export interface NetworkHeaderProps {
  world: WorldLens;
  onWorldChange: (world: WorldLens) => void;
  requests: readonly NetworkRequestRef[];
  requestId: string | null;
  onRequestChange: (requestId: string) => void;
  counts: ParticipantCounts | null;
  /** The world's mode, once known. */
  mode: "live" | "replay" | null;
  now: number;
}

/** The header strip: request picker and counts on the left, lens and world on the right. */
export function NetworkHeader({
  world,
  onWorldChange,
  requests,
  requestId,
  onRequestChange,
  counts,
  mode,
  now,
}: NetworkHeaderProps) {
  const parts = counts !== null && requestId !== null ? countsLine(counts) : [];
  return (
    <header className="@container flex h-[52px] shrink-0 items-center gap-3 border-b border-border bg-canvas px-4">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <RequestPicker requests={requests} requestId={requestId} onChange={onRequestChange} now={now} />
        {parts.length > 0 && (
          // Measured on the header itself. Once there's room for them the picker's title
          // gives way first (`shrink-[4]`), so the counts stay readable.
          <p className="hidden min-w-0 truncate text-ui-sm text-muted @min-[640px]:block" aria-live="polite">
            {parts.map((part, index) => (
              <span key={part}>
                {index > 0 && <span className="px-1.5 text-faint">·</span>}
                <span className={cx(part.includes("human") && "font-medium text-warning")}>{part}</span>
              </span>
            ))}
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2.5">
        <ScopeBadge world={world} />
        {mode !== null && <WorldBadge mode={mode} />}
        <WorldSwitch world={world} onChange={onWorldChange} />
      </div>
    </header>
  );
}
