import {
  BriefcaseBusiness,
  CodeXml,
  FolderClosed,
  Inbox,
  MessageCircle,
  PanelLeft,
  Plus,
  Search,
  Shapes,
  Waypoints,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { NavLink, useMatch } from "react-router";

import type { MemberView } from "../api/types";
import { useInbox } from "../app/queries";
import { AccountFooter } from "./AccountMenu";
import { Recents } from "./Recents";
import { toggleSidebar, useSidebarCollapsed } from "./sidebarStore";
import { Tooltip, cx } from "./ui";
import { useUnavailable } from "./unavailable";

const ICON = { size: 18, strokeWidth: 1.5 } as const;

interface SidebarProps {
  /** The acting member; null before the visitor has a session. */
  member: MemberView | null;
  /** Pin the rail or the full sidebar regardless of the stored preference (gallery). */
  collapsed?: boolean;
  /** Open the account menu on mount (gallery). */
  defaultMenuOpen?: boolean;
  /** No member because the API can't be reached, rather than because nothing started. */
  offline?: boolean;
}

/**
 * Claude's sidebar (UI_REFERENCE.md §2, ARCHITECTURE.md §12): wordmark, New chat ·
 * Search · Customize, then Chats · Requests (badge) · Network · Projects · Artifacts ·
 * Code, Recents, and the account footer. Collapses to a 48px icon rail.
 */
export function Sidebar({ member, collapsed: pinned, defaultMenuOpen = false, offline = false }: SidebarProps) {
  const stored = useSidebarCollapsed();
  const collapsed = pinned ?? stored;
  const unavailable = useUnavailable();
  const live = member !== null;

  return (
    <nav
      aria-label="Sidebar"
      style={{ width: collapsed ? "var(--sidebar-rail-width)" : "var(--sidebar-width)" }}
      className="flex h-full shrink-0 flex-col border-r border-border bg-sidebar transition-[width] duration-200 ease-out"
    >
      <div className={cx("flex h-12 shrink-0 items-center", collapsed ? "justify-center" : "justify-between pr-2 pl-4")}>
        {!collapsed && (
          <NavLink to="/" className="font-serif text-[21px] leading-none font-medium tracking-[-0.015em] text-ink">
            Claude
          </NavLink>
        )}
        <Tooltip content={collapsed ? "Open sidebar" : "Close sidebar"} side="right">
          <button
            type="button"
            aria-label={collapsed ? "Open sidebar" : "Close sidebar"}
            aria-expanded={!collapsed}
            onClick={toggleSidebar}
            className="inline-flex size-8 items-center justify-center rounded-control text-muted transition-colors hover:bg-hover hover:text-ink"
          >
            <PanelLeft {...ICON} />
          </button>
        </Tooltip>
      </div>

      <div className={cx("flex min-h-0 flex-1 flex-col", collapsed ? "px-1.5" : "px-2")}>
        <ul className="flex flex-col gap-px pt-1.5">
          <NavRow to="/" end label="New chat" collapsed={collapsed} icon={<NewChatGlyph />} showActive={false} />
          <ActionRow label="Search" collapsed={collapsed} Icon={Search} onClick={() => { unavailable("Search"); }} />
          <NavRow to="/customize/harmony" label="Customize" collapsed={collapsed} Icon={BriefcaseBusiness} />
        </ul>

        <ul className="mt-3.5 flex flex-col gap-px">
          <NavRow to="/chats" label="Chats" collapsed={collapsed} Icon={MessageCircle} />
          <NavRow
            to="/requests"
            label="Requests"
            collapsed={collapsed}
            Icon={Inbox}
            badge={live ? <RequestsBadge collapsed={collapsed} /> : null}
          />
          <NavRow to="/network" label="Network" collapsed={collapsed} Icon={Waypoints} />
          <ActionRow label="Projects" collapsed={collapsed} Icon={FolderClosed} onClick={() => { unavailable("Projects"); }} />
          <ActionRow label="Artifacts" collapsed={collapsed} Icon={Shapes} onClick={() => { unavailable("Artifacts"); }} />
          <ActionRow label="Code" collapsed={collapsed} Icon={CodeXml} onClick={() => { unavailable("Claude Code"); }} />
        </ul>

        {!collapsed && live && <Recents />}
        {(collapsed || !live) && <div className="flex-1" />}
      </div>

      <AccountFooter member={member} collapsed={collapsed} defaultMenuOpen={defaultMenuOpen} offline={offline} />
    </nav>
  );
}

/** "New chat"'s plus sits in a tinted circle. */
function NewChatGlyph() {
  return (
    <span className="flex size-[22px] items-center justify-center rounded-full bg-pressed text-ink transition-colors group-hover:bg-border-strong">
      <Plus size={14} strokeWidth={1.75} />
    </span>
  );
}

const ROW = "group relative flex h-8 w-full items-center rounded-control text-sm leading-5 text-ink transition-colors duration-100";

interface RowProps {
  label: string;
  collapsed: boolean;
  Icon?: LucideIcon;
  icon?: ReactNode;
  badge?: ReactNode;
}

function RowBody({ label, collapsed, Icon, icon, badge }: RowProps) {
  return (
    <>
      <span className="flex size-[22px] shrink-0 items-center justify-center text-secondary group-hover:text-ink">
        {icon ?? (Icon !== undefined && <Icon {...ICON} />)}
      </span>
      {!collapsed && <span className="ml-2.5 min-w-0 flex-1 truncate text-left">{label}</span>}
      {badge}
    </>
  );
}

function NavRow({
  to,
  end = false,
  showActive = true,
  ...row
}: RowProps & { to: string; end?: boolean; showActive?: boolean }) {
  // `/requests/:threadId` keeps Requests selected. New chat is an action, so it never
  // shows as the current page, as in the Claude app.
  const active = useMatch({ path: to, end }) !== null && showActive;
  return (
    <li>
      <Tooltip content={row.label} side="right" disabled={!row.collapsed}>
        <NavLink
          to={to}
          end={end}
          aria-label={row.collapsed ? row.label : undefined}
          className={cx(ROW, row.collapsed ? "justify-center" : "px-[5px]", active ? "bg-hover" : "hover:bg-hover")}
        >
          <RowBody {...row} />
        </NavLink>
      </Tooltip>
    </li>
  );
}

/** Items outside the prototype: they open the not-available dialog. */
function ActionRow({ onClick, ...row }: RowProps & { onClick: () => void }) {
  return (
    <li>
      <Tooltip content={row.label} side="right" disabled={!row.collapsed}>
        <button
          type="button"
          onClick={onClick}
          aria-label={row.collapsed ? row.label : undefined}
          className={cx(ROW, row.collapsed ? "justify-center" : "px-[5px]", "hover:bg-hover")}
        >
          <RowBody {...row} />
        </button>
      </Tooltip>
    </li>
  );
}

/** Items that need the acting member: the Requests inbox's "Needs you" count. */
function RequestsBadge({ collapsed }: { collapsed: boolean }) {
  const inbox = useInbox();
  const count = inbox.data?.needs_you.length ?? 0;
  if (count === 0) return null;
  if (collapsed) {
    return (
      <span
        aria-label={`${count} need you`}
        className="absolute top-1 right-1.5 size-2 rounded-full bg-accent-strong ring-2 ring-sidebar"
      />
    );
  }
  return (
    <span
      aria-label={`${count} need you`}
      className="ml-2 inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-accent-strong px-1.5 text-[11px] leading-none font-semibold text-white tabular-nums"
    >
      {count > 99 ? "99+" : count}
    </span>
  );
}
