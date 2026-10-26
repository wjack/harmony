import { Plus, Search } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { useConversations } from "../app/queries";
import { timeAgo } from "./time";
import { Button } from "./ui";
import { useUnavailable } from "./unavailable";

/** /chats: the chat history list (the Claude app's "Chats" page). */
export function ChatsPage() {
  const conversations = useConversations();
  const navigate = useNavigate();
  const unavailable = useUnavailable();
  const [query, setQuery] = useState("");

  const needle = query.trim().toLowerCase();
  const rows = (conversations.data ?? []).filter((row) => row.title.toLowerCase().includes(needle));

  return (
    <div className="mx-auto w-full max-w-[840px] px-6 pt-14 pb-16">
      <header className="flex items-center justify-between gap-4">
        <h1 className="font-serif text-[28px] leading-9 font-normal tracking-[-0.015em] text-ink">Chats</h1>
        <Button
          variant="primary"
          size="lg"
          icon={<Plus size={16} strokeWidth={2} />}
          className="h-[34px] gap-1.5 pr-3.5 pl-2.5"
          onClick={() => {
            void navigate("/");
          }}
        >
          New chat
        </Button>
      </header>

      <label className="mt-6 flex h-[42px] items-center gap-2.5 rounded-menu border border-border-strong bg-surface px-3.5 shadow-[0_1px_2px_rgb(20_20_19/0.03)] transition-colors focus-within:border-muted">
        <Search size={18} strokeWidth={1.5} className="shrink-0 text-muted" aria-hidden />
        <input
          type="search"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
          }}
          placeholder="Search your chats..."
          aria-label="Search your chats"
          className="h-full min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none placeholder:text-muted"
        />
      </label>

      <div className="mt-4 flex items-center gap-3 px-3.5 pb-4 text-sm text-muted">
        <span>Your chats with Claude</span>
        <button
          type="button"
          className="text-link underline decoration-link/40 underline-offset-[3px] hover:decoration-link"
          onClick={() => {
            unavailable("Selecting chats");
          }}
        >
          Select
        </button>
      </div>

      {conversations.isError ? (
        <p role="alert" className="border-t border-border px-3.5 py-6 text-sm text-muted">
          Couldn&apos;t load your chats. {conversations.error.message}
        </p>
      ) : (
        <ul className="border-t border-border">
          {rows.map((row) => (
            <li key={row.id} className="border-b border-border">
              <Link
                to={`/chat/${row.id}`}
                className="flex flex-col gap-0.5 rounded-control px-3.5 py-[15px] transition-colors hover:bg-hover"
              >
                <span className="truncate text-sm leading-5 text-ink">{row.title}</span>
                <span className="flex items-center gap-2 text-xs leading-4 text-muted">
                  Last message {timeAgo(row.last_activity_at)}
                  {row.has_pending_card && (
                    <span className="rounded-[4px] bg-accent-soft px-1.5 text-[11px] leading-4 font-medium text-accent-strong">
                      Waiting on you
                    </span>
                  )}
                </span>
              </Link>
            </li>
          ))}
          {conversations.isSuccess && rows.length === 0 && (
            <li className="px-3.5 py-6 text-sm text-muted">
              {needle === "" ? "No chats yet. Start one from New chat." : `No chats match “${query.trim()}”.`}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
