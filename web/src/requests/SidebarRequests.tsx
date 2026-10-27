/**
 * Requests in the sidebar (LIVE_DEMO_PLAN 3.9), the way Claude lists recent chats, above
 * Recents in the same scroll area: "Needs you", every item waiting on the acting member,
 * then "Recent requests", their other threads newest first, and "All requests".
 */
import { NavLink } from "react-router";

import { useInbox, useSession } from "../app/queries";
import { cx } from "../shell/ui";
import { recentRequests, requestPath, threadPath } from "./sections";
import { isUnread, useSeen } from "./seen";
import type { InboxItem } from "./types";

const HEADING = "px-[7px] pb-1.5 text-xs leading-4 text-muted";
const ROW = "flex rounded-control pl-[7px] text-sm leading-5 text-ink transition-colors duration-100";

export function SidebarRequests() {
  const inbox = useInbox();
  const { seen } = useSeen(useSession().data?.member.id);
  if (inbox.data === undefined) return null;
  const needs = inbox.data.needs_you;
  const recent = recentRequests(inbox.data);
  if (needs.length === 0 && recent.length === 0) return null;

  const all = (
    <li>
      <NavLink to="/requests" end className={({ isActive }) => cx(ROW, "h-8 items-center text-secondary", isActive ? "bg-hover" : "hover:bg-hover")}>
        All requests
      </NavLink>
    </li>
  );

  return (
    <>
      {needs.length > 0 && (
        <section aria-labelledby="needs-you-heading" className="mb-4">
          <h2 id="needs-you-heading" className={HEADING}>
            Needs you
          </h2>
          <ul className="flex flex-col gap-px">
            {needs.map((item) => (
              <NeedsRow
                key={item.thread_id}
                item={item}
                unread={isUnread(seen, item.thread_id, item.last_activity_at)}
              />
            ))}
            {recent.length === 0 && all}
          </ul>
        </section>
      )}
      {recent.length > 0 && (
        <section aria-labelledby="recent-requests-heading" className="mb-4">
          <h2 id="recent-requests-heading" className={HEADING}>
            Recent requests
          </h2>
          <ul className="flex flex-col gap-px">
            {recent.map((item) => (
              <RecentRow key={item.thread_id} item={item} />
            ))}
            {all}
          </ul>
        </section>
      )}
    </>
  );
}

/**
 * Two lines: a warning dot and the title, then who it's with; a dot at the end while
 * unread, which the link's name says too (a dot alone says nothing to a screen reader).
 */
function NeedsRow({ item, unread }: { item: InboxItem; unread: boolean }) {
  return (
    <li>
      <NavLink
        to={requestPath(item, "needs_you")}
        aria-label={`${item.title}, with ${item.counterparty.name}${unread ? ", unread" : ""}`}
        className={({ isActive }) =>
          cx(ROW, "relative flex-col gap-px py-1.5 pr-6", isActive ? "bg-hover" : "hover:bg-hover")
        }
      >
        <span className="flex min-w-0 items-center gap-2">
          <span aria-hidden className="size-1.5 shrink-0 rounded-full bg-warning" />
          <span className={cx("min-w-0 flex-1 truncate", unread && "font-medium")}>{item.title}</span>
        </span>
        <span className="truncate pl-3.5 text-[12.5px] leading-[18px] text-muted">{item.counterparty.name}</span>
        {unread && <span aria-hidden className="absolute top-3 right-3 size-1.5 rounded-full bg-accent" />}
      </NavLink>
    </li>
  );
}

/** One line, the title; who it's with is in the tooltip and the link's name. */
function RecentRow({ item }: { item: InboxItem }) {
  return (
    <li>
      <NavLink
        to={threadPath(item)}
        title={`${item.title} · ${item.counterparty.name}`}
        aria-label={`${item.title}, with ${item.counterparty.name}`}
        className={({ isActive }) => cx(ROW, "h-8 items-center pr-2", isActive ? "bg-hover" : "hover:bg-hover")}
      >
        <span className="min-w-0 flex-1 truncate">{item.title}</span>
      </NavLink>
    </li>
  );
}
