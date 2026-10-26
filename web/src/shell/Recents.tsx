import { Ellipsis } from "lucide-react";
import { NavLink } from "react-router";

import type { ConversationSummary } from "../api/types";
import { useConversations } from "../app/queries";
import { cx } from "./ui";
import { useUnavailable } from "./unavailable";

const MAX_RECENTS = 30;

/** The sidebar's Recents: the acting member's chats, most recent first. */
export function Recents() {
  const conversations = useConversations();
  const items = conversations.data?.slice(0, MAX_RECENTS) ?? [];

  return (
    <section aria-labelledby="recents-heading" className="mt-4 flex min-h-0 flex-1 flex-col">
      <h2 id="recents-heading" className="px-[7px] pb-1.5 text-xs leading-4 text-muted">
        Recents
      </h2>
      <ul className="scrollbar-quiet -mr-2 flex min-h-0 flex-1 flex-col gap-px overflow-y-auto pr-2 pb-2">
        {items.map((conversation) => (
          <RecentRow key={conversation.id} conversation={conversation} />
        ))}
        {conversations.isSuccess && items.length === 0 && (
          <li className="px-[7px] py-1.5 text-[13px] leading-5 text-faint">Your chats will show up here.</li>
        )}
      </ul>
    </section>
  );
}

function RecentRow({ conversation }: { conversation: ConversationSummary }) {
  const unavailable = useUnavailable();
  return (
    <li className="group/recent relative">
      <NavLink
        to={`/chat/${conversation.id}`}
        title={conversation.title}
        className={({ isActive }) =>
          cx(
            "flex h-8 items-center rounded-control pr-8 pl-[7px] text-sm leading-5 text-ink transition-colors duration-100",
            isActive ? "bg-hover" : "hover:bg-hover",
          )
        }
      >
        <span className="min-w-0 flex-1 truncate">{conversation.title}</span>
      </NavLink>
      {conversation.has_pending_card && (
        <span
          aria-label="Waiting on you"
          className="pointer-events-none absolute top-1/2 right-3 size-1.5 -translate-y-1/2 rounded-full bg-accent group-hover/recent:opacity-0"
        />
      )}
      <button
        type="button"
        aria-label={`Options for ${conversation.title}`}
        onClick={() => {
          unavailable("Chat options");
        }}
        className="absolute top-1/2 right-1 inline-flex size-6 -translate-y-1/2 items-center justify-center rounded-[6px] text-muted opacity-0 transition-opacity group-hover/recent:opacity-100 hover:bg-pressed hover:text-ink focus-visible:opacity-100"
      >
        <Ellipsis size={16} strokeWidth={1.75} />
      </button>
    </li>
  );
}
