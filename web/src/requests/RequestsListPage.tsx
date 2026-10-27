/**
 * `/requests` (LIVE_DEMO_PLAN 3.9): every request the member can see, searchable, in the
 * shape of Claude's Chats page, grouped Needs you · With your team · Claude is handling
 * · Shared with you · Done. A row reads its title, then who it's with, where it stands
 * and how long ago it moved.
 */
import { Search } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router";

import { useCalendar } from "../demo/worldClock";
import { useInbox, useSession } from "../app/queries";
import { Button, OrgAvatar, Segmented, Spark, cx } from "../shell/ui";
import { rowStatus, shortAge } from "./format";
import { DIRECTIONS, SECTIONS, matches, requestPath, type Direction } from "./sections";
import { isUnread, useSeen } from "./seen";
import type { InboxItem, InboxSection } from "./types";

export function RequestsListPage() {
  const memberId = useSession().data?.member.id;
  const inbox = useInbox();
  const { seen } = useSeen(memberId);
  const [query, setQuery] = useState("");
  const [direction, setDirection] = useState<Direction>("all");

  const filtered = query.trim() !== "" || direction !== "all";
  const { data } = inbox;
  const groups =
    data === undefined
      ? []
      : SECTIONS.map((section) => ({
          ...section,
          items: data[section.key].filter((item) => matches(item, query, direction)),
        })).filter((group) => group.items.length > 0);

  let list: ReactNode;
  if (inbox.isPending) {
    list = <ListSkeleton />;
  } else if (inbox.isError) {
    list = (
      <div role="alert" className="flex flex-col items-start gap-3 border-t border-border px-3.5 py-6">
        <p className="text-sm text-ink">Couldn&apos;t load your requests.</p>
        <p className="text-[13px] text-muted">{inbox.error.message}</p>
        <Button
          size="sm"
          onClick={() => {
            void inbox.refetch();
          }}
        >
          Retry
        </Button>
      </div>
    );
  } else if (groups.length === 0) {
    list = <EmptyList filtered={filtered} />;
  } else {
    list = groups.map((group) => (
      <section key={group.key} aria-labelledby={`requests-${group.key}`} className="mt-6 first:mt-0">
        <h2 id={`requests-${group.key}`} className="px-3.5 pb-2 text-[13px] text-muted">
          {group.label} <span className="text-faint tabular-nums">· {group.items.length}</span>
        </h2>
        <ul className="border-t border-border">
          {group.items.map((item) => (
            <li key={`${group.key}:${item.thread_id}`} className="border-b border-border">
              <ListRow
                item={item}
                section={group.key}
                unread={group.key !== "done" && isUnread(seen, item.thread_id, item.last_activity_at)}
              />
            </li>
          ))}
        </ul>
      </section>
    ));
  }

  return (
    <div className="mx-auto w-full max-w-[840px] px-6 pt-14 pb-16">
      <h1 className="font-serif text-[28px] leading-9 font-normal tracking-[-0.015em] text-ink">Requests</h1>

      <label className="mt-6 flex h-[42px] items-center gap-2.5 rounded-menu border border-border-strong bg-surface px-3.5 shadow-[0_1px_2px_rgb(20_20_19/0.03)] transition-colors focus-within:border-muted">
        <Search size={18} strokeWidth={1.5} className="shrink-0 text-muted" aria-hidden />
        <input
          type="search"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
          }}
          placeholder="Search requests..."
          aria-label="Search requests"
          className="h-full min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none placeholder:text-muted"
        />
      </label>

      <div className="mt-4 mb-5 flex flex-wrap items-center justify-between gap-3 px-3.5">
        <span className="text-sm text-muted">Requests between your organization and others</span>
        <Segmented label="Show" value={direction} options={DIRECTIONS} onChange={setDirection} />
      </div>

      {list}
    </div>
  );
}

function ListRow({ item, section, unread }: { item: InboxItem; section: InboxSection; unread: boolean }) {
  const calendar = useCalendar();
  const status = rowStatus(item, section === "needs_you");
  return (
    <Link
      to={requestPath(item, section)}
      className="flex items-center gap-3 rounded-control px-3.5 py-3 transition-colors hover:bg-hover"
    >
      <OrgAvatar name={item.counterparty.name} seed={item.counterparty.slug} size={28} />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className={cx("truncate text-sm leading-5", unread ? "font-medium text-ink" : "text-ink")}>{item.title}</span>
        <span className="truncate text-xs leading-4 text-muted">
          {item.counterparty.name} · {status} · {shortAge(item.last_activity_at, calendar)}
        </span>
      </span>
      {unread && (
        <>
          <span aria-hidden className="size-2 shrink-0 rounded-full bg-accent" />
          {/* An aria-label on a plain span is never read: the link's name says it. */}
          <span className="sr-only">, unread</span>
        </>
      )}
    </Link>
  );
}

function EmptyList({ filtered }: { filtered: boolean }) {
  return (
    <div className="flex flex-col items-center border-t border-border px-6 pt-14 text-center">
      <Spark size={22} />
      <p className="mt-3 text-[14px] font-medium text-ink">{filtered ? "No matching requests" : "You're all caught up"}</p>
      <p className="mt-1 max-w-sm text-[13px] leading-5 text-muted">
        {filtered
          ? "Try a different search or filter."
          : "When another organization's Claude needs you, or Claude routes something your way, it shows up here."}
      </p>
    </div>
  );
}

function ListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading requests" className="flex flex-col border-t border-border">
      {[0.62, 0.48, 0.55, 0.4].map((width) => (
        <div key={width} className="flex items-center gap-3 border-b border-border px-3.5 py-3">
          <span className="size-7 shrink-0 animate-pulse rounded-[7px] bg-hover" />
          <span className="flex flex-1 flex-col gap-2">
            <span className="h-3 animate-pulse rounded bg-hover" style={{ width: `${String(width * 100)}%` }} />
            <span className="h-2.5 w-1/3 animate-pulse rounded bg-hover" />
          </span>
        </div>
      ))}
    </div>
  );
}
