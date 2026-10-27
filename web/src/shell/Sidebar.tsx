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
import { useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { NavLink, useLocation, useMatch } from "react-router";

import type { MemberView } from "../api/types";
import { useInbox } from "../app/queries";
import { SidebarRequests } from "../requests/SidebarRequests";
import { AccountFooter } from "./AccountMenu";
import { Recents } from "./Recents";
import { toggleSidebar, useSidebarCollapsed } from "./sidebarStore";
import { Tooltip, cx } from "./ui";
import { useUnavailable } from "./unavailable";
import { useMediaQuery } from "./useMediaQuery";

const ICON = { size: 18, strokeWidth: 1.5 } as const;

/** No room for the sidebar beside the page: it shows the rail, and opens over the page. */
const PHONE = "(max-width: 767px)";

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
 * Code; the member's requests and Recents, scrolling together (LIVE_DEMO_PLAN 3.9); and
 * the account footer. Collapses to a 48px icon rail, which keeps only the badge.
 *
 * On a phone it is the rail whatever the stored choice, and opening it lays it over the
 * page, for the page it was opened on: following a link, the scrim or Escape closes it.
 */
export function Sidebar({ member, collapsed: pinned, defaultMenuOpen = false, offline = false }: SidebarProps) {
  const stored = useSidebarCollapsed();
  const phone = useMediaQuery(PHONE) && pinned === undefined;
  const { pathname } = useLocation();
  const [openOn, setOpenOn] = useState<string | null>(null);
  const over = phone && openOn === pathname;
  const collapsed = pinned ?? (phone ? !over : stored);
  const unavailable = useUnavailable();
  const live = member !== null;

  const close = () => {
    setOpenOn(null);
  };
  const toggle = phone
    ? () => {
        setOpenOn(over ? null : pathname);
      }
    : toggleSidebar;
  // A link to the page already open leaves the path as it is.
  const onClick = (event: MouseEvent<HTMLElement>) => {
    if (over && event.target instanceof Element && event.target.closest("a") !== null) close();
  };
  // Not from the account menu, which portals out of the sidebar and closes itself.
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (over && event.key === "Escape" && event.target instanceof Node && event.currentTarget.contains(event.target)) {
      close();
    }
  };

  return (
    <>
      {/* Over the page, the sidebar leaves the rail's room behind it and dims the rest. */}
      {over && <div aria-hidden className="w-(--sidebar-rail-width) shrink-0" />}
      {over && <div aria-hidden onClick={close} className="absolute inset-0 z-30 bg-scrim animate-fade-in" />}
      <nav
        aria-label="Sidebar"
        style={{ width: collapsed ? "var(--sidebar-rail-width)" : "var(--sidebar-width)" }}
        onClick={onClick}
        onKeyDown={onKeyDown}
        className={cx(
          "flex h-full shrink-0 flex-col border-r border-border bg-sidebar transition-[width] duration-200 ease-out",
          over && "absolute inset-y-0 left-0 z-40 shadow-modal",
        )}
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
              onClick={toggle}
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

          {!collapsed && live ? (
            <div className="scrollbar-quiet -mr-2 mt-4 flex min-h-0 flex-1 flex-col overflow-y-auto pr-2 pb-2">
              <SidebarRequests />
              <Recents />
            </div>
          ) : (
            <div className="flex-1" />
          )}
        </div>

        <AccountFooter member={member} collapsed={collapsed} defaultMenuOpen={defaultMenuOpen} offline={offline} />
      </nav>
    </>
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
