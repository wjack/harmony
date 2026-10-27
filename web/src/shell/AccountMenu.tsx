import {
  ChevronRight,
  ChevronsUpDown,
  CircleArrowUp,
  CircleHelp,
  Download,
  Gift,
  Globe,
  Info,
  Lock,
  RotateCcw,
  Settings,
  SlidersHorizontal,
  Users,
} from "lucide-react";
import { useState } from "react";

import type { MemberView } from "../api/types";
import { useLockHarmony } from "../app/lock";
import { useDemoStory, useMembers } from "../app/queries";
import { useSwitchAccount } from "../demo/accounts";
import { menuAccounts } from "../demo/cast";
import { openDemoControls, openResetConfirm } from "../demo/store";
import { memberSubtitle } from "./names";
import {
  Avatar,
  Menu,
  MenuCheckItem,
  MenuContent,
  MenuGroup,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuSub,
  MenuSubContent,
  MenuSubTrigger,
  MenuTrigger,
  Tooltip,
  cx,
} from "./ui";
import { useUnavailable } from "./unavailable";

const ICON = { size: 16, strokeWidth: 1.5 } as const;

interface AccountFooterProps {
  member: MemberView | null;
  collapsed: boolean;
  /** Open on mount, and non-modal, so the gallery can show it in place. */
  defaultMenuOpen?: boolean;
  /** Without a member: the API can't be reached (rather than "not started"). */
  offline?: boolean;
}

/**
 * The sidebar footer: avatar, name and plan line (for Harmony, the org), a download
 * button and the up/down chevron. The whole identity block opens the account menu.
 */
export function AccountFooter({ member, collapsed, defaultMenuOpen = false, offline = false }: AccountFooterProps) {
  const unavailable = useUnavailable();
  const [open, setOpen] = useState(defaultMenuOpen);

  if (member === null) {
    return (
      <div className={cx("flex h-[60px] shrink-0 items-center border-t border-border", collapsed ? "justify-center" : "px-3")}>
        {!collapsed && <p className="text-[13px] leading-5 text-muted">{offline ? "Connecting…" : "Not started"}</p>}
      </div>
    );
  }

  const download = (
    <button
      type="button"
      aria-label="Get apps and extensions"
      onClick={() => {
        unavailable("Get apps and extensions");
      }}
      className={cx(
        "relative inline-flex shrink-0 items-center justify-center rounded-control text-secondary transition-colors hover:bg-hover hover:text-ink",
        collapsed ? "size-8" : "size-[30px] border border-border-strong bg-surface",
      )}
    >
      <Download size={15} strokeWidth={1.5} />
      <span className="absolute -top-0.5 -right-0.5 size-2 rounded-full bg-toggle ring-2 ring-sidebar" />
    </button>
  );

  if (collapsed) {
    return (
      <div className="flex shrink-0 flex-col items-center gap-2 pt-2 pb-3">
        {download}
        <Menu open={open} onOpenChange={setOpen} modal={!defaultMenuOpen}>
          <Tooltip content={member.name} side="right">
            <MenuTrigger
              aria-label={`Account: ${member.name}`}
              className="rounded-full outline-offset-2 transition-opacity hover:opacity-90"
            >
              <Avatar name={member.name} seed={member.org.slug} size={32} />
            </MenuTrigger>
          </Tooltip>
          <AccountMenuContent member={member} side="right" pinned={defaultMenuOpen} />
        </Menu>
      </div>
    );
  }

  return (
    <div className="flex h-[60px] shrink-0 items-center gap-1 border-t border-border px-2">
      <Menu open={open} onOpenChange={setOpen} modal={!defaultMenuOpen}>
        <MenuTrigger
          aria-label={`Account: ${member.name}`}
          className={cx(
            "flex min-w-0 flex-1 items-center gap-2.5 rounded-control px-1.5 py-1.5 text-left transition-colors hover:bg-hover",
            open && "bg-hover",
          )}
        >
          <Avatar name={member.name} seed={member.org.slug} size={32} />
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="truncate text-sm leading-[18px] font-medium text-ink">{member.name}</span>
            <span className="truncate text-xs leading-4 text-muted">{member.org.name}</span>
          </span>
        </MenuTrigger>
        <AccountMenuContent member={member} side="top" pinned={defaultMenuOpen} />
      </Menu>
      {download}
      <button
        type="button"
        aria-label="Open account menu"
        onClick={() => {
          setOpen(true);
        }}
        className="inline-flex h-8 w-5 shrink-0 items-center justify-center rounded-control text-muted transition-colors hover:text-ink"
      >
        <ChevronsUpDown size={14} strokeWidth={1.75} />
      </button>
    </div>
  );
}

/** The menu keeps clear of the demo bar, so "Demo controls" and "Acting as" stay in view. */
function demoBarClearance(): number {
  const height = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--demo-bar-height"));
  return (Number.isFinite(height) ? height : 0) + 8;
}

/**
 * Claude's account menu (UI_REFERENCE.md §5) with Harmony's additions: after the
 * identity line, a "Switch account" group of the walkthrough's people (✓ on the active
 * one, every other network member in a submenu), then Demo controls and Reset demo.
 * Everything else is Claude's own and opens the not-available dialog.
 */
export function AccountMenuContent({
  member,
  side,
  pinned = false,
}: {
  member: MemberView;
  side: "top" | "right";
  /** Gallery-only: stay open when focus moves elsewhere, and hide while scrolled away. */
  pinned?: boolean;
}) {
  const members = useMembers();
  const story = useDemoStory();
  const { switchTo } = useSwitchAccount();
  const lockHarmony = useLockHarmony();
  const unavailable = useUnavailable();
  const { shown, more } = menuAccounts(members.data, story.data, member.id);

  const choose = (target: MemberView) => {
    if (target.id !== member.id) void switchTo(target);
  };

  return (
    <MenuContent
      side={side}
      align={side === "top" ? "start" : "end"}
      hideWhenDetached={pinned}
      collisionPadding={{ top: demoBarClearance(), right: 8, bottom: 8, left: 8 }}
      onFocusOutside={
        pinned
          ? (event) => {
              event.preventDefault();
            }
          : undefined
      }
      // Above the footer it spans the sidebar, inset 8px each side, as Claude's does.
      className={cx(
        "scrollbar-quiet max-h-[var(--radix-dropdown-menu-content-available-height)] overflow-y-auto",
        side === "top" ? "w-[calc(var(--sidebar-width)-16px)]" : "w-[284px]",
      )}
    >
      <MenuLabel>Acting as {member.name}</MenuLabel>

      <MenuGroup aria-label="Switch account">
        <p className="px-2 pt-2 pb-1 text-[11px] leading-4 font-medium tracking-[0.04em] text-faint uppercase">
          Switch account
        </p>
        {members.isPending && <p className="px-2 py-1.5 text-[13px] text-muted">Loading accounts…</p>}
        {shown.map((candidate) => (
          <MemberRow key={candidate.id} member={candidate} active={candidate.id === member.id} onSelect={choose} />
        ))}
        {more.length > 0 && (
          <MenuSub>
            <MenuSubTrigger icon={<Users {...ICON} />}>All network members</MenuSubTrigger>
            <MenuSubContent className="scrollbar-quiet max-h-[min(420px,var(--radix-dropdown-menu-content-available-height))] w-[284px] overflow-y-auto">
              {more.map((candidate) => (
                <MemberRow key={candidate.id} member={candidate} active={candidate.id === member.id} onSelect={choose} />
              ))}
            </MenuSubContent>
          </MenuSub>
        )}
      </MenuGroup>

      <MenuSeparator />
      <MenuItem
        icon={<SlidersHorizontal {...ICON} />}
        onSelect={() => {
          openDemoControls("controls");
        }}
      >
        Demo controls
      </MenuItem>
      <MenuItem icon={<RotateCcw {...ICON} />} onSelect={openResetConfirm}>
        Reset demo
      </MenuItem>

      <MenuSeparator />
      <MenuItem icon={<Settings {...ICON} />} shortcut="⇧⌘," onSelect={() => { unavailable("Settings"); }}>
        Settings
      </MenuItem>
      <MenuItem icon={<Globe {...ICON} />} onSelect={() => { unavailable("Language"); }}>
        <span className="flex items-center justify-between">
          Language
          <ChevronRight size={16} strokeWidth={1.5} className="text-muted" aria-hidden />
        </span>
      </MenuItem>
      <MenuItem icon={<CircleHelp {...ICON} />} onSelect={() => { unavailable("Get help"); }}>
        Get help
      </MenuItem>

      <MenuSeparator />
      <MenuItem icon={<CircleArrowUp {...ICON} />} onSelect={() => { unavailable("Upgrade plan"); }}>
        Upgrade plan
      </MenuItem>
      <MenuItem icon={<Download {...ICON} />} onSelect={() => { unavailable("Get apps and extensions"); }}>
        Get apps and extensions
      </MenuItem>
      <MenuItem icon={<Gift {...ICON} />} onSelect={() => { unavailable("Gift Claude"); }}>
        Gift Claude
      </MenuItem>
      <MenuItem icon={<Info {...ICON} />} onSelect={() => { unavailable("Learn more"); }}>
        <span className="flex items-center justify-between">
          Learn more
          <ChevronRight size={16} strokeWidth={1.5} className="text-muted" aria-hidden />
        </span>
      </MenuItem>

      <MenuSeparator />
      <MenuItem
        icon={<Lock {...ICON} />}
        onSelect={() => {
          lockHarmony.mutate();
        }}
      >
        Lock Harmony
      </MenuItem>
    </MenuContent>
  );
}

function MemberRow({
  member,
  active,
  onSelect,
}: {
  member: MemberView;
  active: boolean;
  onSelect: (member: MemberView) => void;
}) {
  return (
    <MenuCheckItem
      checked={active}
      icon={<Avatar name={member.name} seed={member.org.slug} size={24} />}
      onSelect={() => {
        onSelect(member);
      }}
      className="py-1"
    >
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[13.5px] leading-[18px] text-ink">{member.name}</span>
        <span className="truncate text-xs leading-4 text-muted">{memberSubtitle(member)}</span>
      </span>
    </MenuCheckItem>
  );
}
