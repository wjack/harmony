/**
 * The inbox's list pane (UI_REFERENCE §7): Linear's inbox rows in Claude's tokens,
 * grouped Needs you · With your team · Claude is handling · Shared with you · Done.
 */
import { Check, ChevronDown, CircleX, ListFilter, Search, X } from "lucide-react";
import { useMemo, useState } from "react";

import {
  Avatar,
  IconButton,
  Menu,
  MenuCheckItem,
  MenuContent,
  MenuLabel,
  MenuTrigger,
  OrgAvatar,
  Spark,
  Tooltip,
  cx,
} from "../shell/ui";
import { useCalendar } from "../demo/worldClock";
import { rowStatus, shortAge } from "./format";
import { isUnread } from "./seen";
import type { Inbox, InboxItem, InboxSection } from "./types";

const SECTIONS: readonly { key: InboxSection; label: string }[] = [
  { key: "needs_you", label: "Needs you" },
  { key: "with_your_team", label: "With your team" },
  { key: "claude_handling", label: "Claude is handling" },
  { key: "shared_with_you", label: "Shared with you" },
  { key: "done", label: "Done" },
];

type Direction = "all" | "inbound" | "outbound";

const DIRECTIONS: readonly { value: Direction; label: string }[] = [
  { value: "all", label: "All requests" },
  { value: "inbound", label: "Inbound: other orgs asking us" },
  { value: "outbound", label: "Outbound: our requests" },
];

function matches(item: InboxItem, query: string, direction: Direction): boolean {
  if (direction === "inbound" && item.side !== "responder") return false;
  if (direction === "outbound" && item.side !== "requester") return false;
  if (query === "") return true;
  const needle = query.toLowerCase();
  return item.title.toLowerCase().includes(needle) || item.counterparty.name.toLowerCase().includes(needle);
}

interface InboxListProps {
  inbox: Inbox;
  selectedThreadId: string | undefined;
  seen: Record<string, string>;
  /** Our org's slug, which tints teammates' avatars as everywhere else. */
  orgSlug: string | undefined;
  onOpen: (threadId: string) => void;
}

export function InboxList({ inbox, selectedThreadId, seen, orgSlug, onOpen }: InboxListProps) {
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState("");
  const [direction, setDirection] = useState<Direction>("all");
  const [collapsed, setCollapsed] = useState<ReadonlySet<InboxSection>>(new Set());

  const groups = useMemo(
    () =>
      SECTIONS.map((section) => ({
        ...section,
        items: inbox[section.key].filter((item) => matches(item, query.trim(), direction)),
      })),
    [inbox, query, direction],
  );
  const total = groups.reduce((sum, group) => sum + group.items.length, 0);
  const filtered = query.trim() !== "" || direction !== "all";

  const toggle = (key: InboxSection) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex h-[52px] shrink-0 items-center gap-1 pr-2.5 pl-4">
        <h1 className="flex-1 text-[15px] font-semibold tracking-[-0.005em]">Requests</h1>
        <Tooltip content="Search" side="bottom">
          <IconButton
            label="Search requests"
            size="sm"
            aria-pressed={searching}
            onClick={() => {
              setSearching((open) => !open);
              setQuery("");
            }}
          >
            <Search size={16} strokeWidth={1.5} />
          </IconButton>
        </Tooltip>
        <Menu>
          <Tooltip content="Filter" side="bottom">
            <MenuTrigger asChild>
              <IconButton label="Filter requests" size="sm" className={cx(direction !== "all" && "text-ink")}>
                <ListFilter size={16} strokeWidth={1.5} />
              </IconButton>
            </MenuTrigger>
          </Tooltip>
          <MenuContent align="end" className="w-[260px]">
            <MenuLabel>Show</MenuLabel>
            {DIRECTIONS.map((option) => (
              <MenuCheckItem
                key={option.value}
                checked={direction === option.value}
                onSelect={() => {
                  setDirection(option.value);
                }}
              >
                {option.label}
              </MenuCheckItem>
            ))}
          </MenuContent>
        </Menu>
      </header>

      {searching && (
        <div className="shrink-0 px-3 pb-2">
          <div className="flex h-8 items-center gap-2 rounded-control border border-border-strong bg-surface px-2.5 focus-within:border-muted">
            <Search size={14} strokeWidth={1.5} className="shrink-0 text-muted" aria-hidden />
            <input
              autoFocus
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
              }}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  setSearching(false);
                  setQuery("");
                }
              }}
              placeholder="Search requests"
              aria-label="Search requests"
              className="min-w-0 flex-1 bg-transparent text-[13px] outline-none placeholder:text-faint"
            />
            {query !== "" && (
              <button
                type="button"
                aria-label="Clear search"
                className="text-muted hover:text-ink"
                onClick={() => {
                  setQuery("");
                }}
              >
                <X size={14} strokeWidth={1.5} />
              </button>
            )}
          </div>
        </div>
      )}

      <nav aria-label="Requests" className="scrollbar-quiet min-h-0 flex-1 overflow-y-auto pb-3">
        {total === 0 ? (
          <EmptyList filtered={filtered} />
        ) : (
          groups.map((group) =>
            group.items.length === 0 ? null : (
              <section key={group.key} aria-label={group.label} className="pt-1.5 pb-1">
                <button
                  type="button"
                  aria-expanded={!collapsed.has(group.key)}
                  onClick={() => {
                    toggle(group.key);
                  }}
                  className="group/section flex w-full items-center gap-1 px-4 py-1.5 text-left text-[12.5px] text-muted hover:text-ink"
                >
                  <ChevronDown
                    size={14}
                    strokeWidth={1.5}
                    aria-hidden
                    className={cx("transition-transform duration-100", collapsed.has(group.key) && "-rotate-90")}
                  />
                  <span>{group.label}</span>
                  <span className="ml-1 text-faint tabular-nums">{group.items.length}</span>
                </button>
                {!collapsed.has(group.key) && (
                  <ul className="flex flex-col gap-px px-2">
                    {group.items.map((item) => (
                      <li key={`${group.key}:${item.thread_id}`}>
                        <InboxRow
                          item={item}
                          section={group.key}
                          selected={item.thread_id === selectedThreadId}
                          unread={group.key !== "done" && isUnread(seen, item.thread_id, item.last_activity_at)}
                          orgSlug={orgSlug}
                          onOpen={onOpen}
                        />
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            ),
          )
        )}
      </nav>
    </div>
  );
}

interface InboxRowProps {
  item: InboxItem;
  section: InboxSection;
  selected: boolean;
  unread: boolean;
  orgSlug: string | undefined;
  onOpen: (threadId: string) => void;
}

export function InboxRow({ item, section, selected, unread, orgSlug, onOpen }: InboxRowProps) {
  const calendar = useCalendar();
  const subtitle = `${item.counterparty.name} · ${rowStatus(item, section === "needs_you")}`;
  return (
    <button
      type="button"
      aria-current={selected ? "page" : undefined}
      onClick={() => {
        onOpen(item.thread_id);
      }}
      className={cx(
        "flex w-full items-start gap-3 rounded-[10px] px-2.5 py-2.5 text-left transition-colors duration-100",
        selected ? "bg-hover" : "hover:bg-hover/70",
      )}
    >
      <OrgAvatar name={item.counterparty.name} seed={item.counterparty.slug} size={30} className="mt-0.5" />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex items-center gap-2">
          {unread && <span aria-label="Unread" className="size-1.5 shrink-0 rounded-full bg-accent" />}
          <span
            className={cx(
              "min-w-0 flex-1 truncate text-[13.5px] leading-5",
              unread || selected ? "font-medium text-ink" : "text-secondary",
            )}
          >
            {item.title}
          </span>
          <span className="shrink-0 text-[12px] text-muted tabular-nums">{shortAge(item.last_activity_at, calendar)}</span>
        </span>
        <span className="flex items-center gap-2">
          <span className="min-w-0 flex-1 truncate text-[12.5px] leading-[18px] text-muted">{subtitle}</span>
          <RowGlyph item={item} section={section} orgSlug={orgSlug} />
        </span>
      </span>
    </button>
  );
}

/**
 * The small status mark at the right of a row's second line. A Needs-you row has none:
 * its group says so, and the leading dot is the row's one mark (unread), as in Linear.
 */
function RowGlyph({ item, section, orgSlug }: { item: InboxItem; section: InboxSection; orgSlug: string | undefined }) {
  if (section === "needs_you") return null;
  if (section === "claude_handling") return <Spark size={13} />;
  // The subtitle names who it waits on or who shared it; the avatar is the scannable
  // mark, as Linear's assignee.
  if (section === "with_your_team" || section === "shared_with_you") {
    const person = section === "with_your_team" ? (item.escalation?.assignees[0] ?? null) : item.shared_by;
    return person === null ? null : <Avatar name={person.name} seed={orgSlug} size={16} />;
  }
  if (item.thread_state === "closed") {
    return <Check size={14} strokeWidth={2} aria-hidden className="shrink-0 text-success" />;
  }
  if (item.thread_state === "expired") {
    return <CircleX size={13} strokeWidth={1.5} aria-hidden className="shrink-0 text-faint" />;
  }
  return null;
}

function EmptyList({ filtered }: { filtered: boolean }) {
  return (
    <div className="flex flex-col items-center px-6 pt-16 text-center">
      <Spark size={22} />
      <p className="mt-3 text-[14px] font-medium text-ink">{filtered ? "No matching requests" : "You're all caught up"}</p>
      <p className="mt-1 text-[13px] leading-5 text-muted">
        {filtered
          ? "Try a different search or filter."
          : "When another organization's Claude needs you, or Claude routes something your way, it shows up here."}
      </p>
    </div>
  );
}
