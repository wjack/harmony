/**
 * Requests in the sidebar (LIVE_DEMO_PLAN 3.9), above Recents in the same scroll area:
 * "Needs you", every item waiting on the acting member, and "All requests". Everything
 * else lives on the Requests page, so the sidebar stays about chats.
 */
import { NavLink } from "react-router";

import { useInbox, useSession } from "../app/queries";
import { cx } from "../shell/ui";
import { requestPath } from "./sections";
import { isUnread, useSeen } from "./seen";
import type { InboxItem } from "./types";

const HEADING = "px-[7px] pb-1.5 text-xs leading-4 text-muted";
const ROW = "flex rounded-control pl-[7px] text-sm leading-5 text-ink transition-colors duration-100";

export function SidebarRequests() {
  const inbox = useInbox();
  const { seen } = useSeen(useSession().data?.member.id);
  if (inbox.data === undefined) return null;
  const needs = inbox.data.needs_you;
  if (needs.length === 0) return null;

  return (
    <section aria-labelledby="needs-you-heading" className="mb-4">
      <h2 id="needs-you-heading" className={HEADING}>
        Needs you
      </h2>
      <ul className="flex flex-col gap-px">
        {needs.map((item) => (
          <NeedsRow key={item.thread_id} item={item} unread={isUnread(seen, item.thread_id, item.last_activity_at)} />
        ))}
        <li>
          <NavLink
            to="/requests"
            end
            className={({ isActive }) =>
              cx(ROW, "h-8 items-center text-secondary", isActive ? "bg-hover" : "hover:bg-hover")
            }
          >
            All requests
          </NavLink>
        </li>
      </ul>
    </section>
  );
}

/**
 * Two lines: the title, then who it's with. The section already says it needs you, so
 * the row's one dot, at its end, means unread, which the link's name says too (a dot
 * alone says nothing to a screen reader).
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
        <span className={cx("min-w-0 truncate", unread && "font-medium")}>{item.title}</span>
        <span className="truncate text-[12.5px] leading-[18px] text-muted">{item.counterparty.name}</span>
        {unread && <span aria-hidden className="absolute top-3 right-3 size-1.5 rounded-full bg-accent" />}
      </NavLink>
    </li>
  );
}
