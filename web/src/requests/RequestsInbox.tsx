/**
 * The Requests inbox (UI_REFERENCE §7): the list pane, the open thread and its rail.
 * Router-free, so the page and the component gallery drive it the same way.
 */
import { Inbox as InboxIcon } from "lucide-react";
import type { ReactNode } from "react";

import { useInbox, useSession } from "../app/queries";
import { Button, cx } from "../shell/ui";
import { InboxList } from "./InboxList";
import { ThreadPane } from "./ThreadPane";
import { useRequestsLive } from "./live";
import { useSeen } from "./seen";
import type { Inbox, InboxItem } from "./types";

export interface RequestsInboxProps {
  threadId: string | undefined;
  onOpenThread: (threadId: string) => void;
  onCloseThread: () => void;
}

function findItem(inbox: Inbox | undefined, threadId: string | undefined): InboxItem | undefined {
  if (inbox === undefined || threadId === undefined) return undefined;
  for (const section of [inbox.needs_you, inbox.with_your_team, inbox.shared_with_you, inbox.claude_handling, inbox.done]) {
    const item = section.find((candidate) => candidate.thread_id === threadId);
    if (item !== undefined) return item;
  }
  return undefined;
}

export function RequestsInbox({ threadId, onOpenThread, onCloseThread }: RequestsInboxProps) {
  const session = useSession();
  const memberId = session.data?.member.id;
  const inbox = useInbox();
  const { seen, markSeen } = useSeen(memberId);
  useRequestsLive(threadId);

  let list: ReactNode;
  if (inbox.isPending) {
    list = <ListSkeleton />;
  } else if (inbox.isError) {
    list = (
      <div className="flex flex-col items-start gap-3 px-4 pt-16">
        <p className="text-[13.5px] text-ink">Couldn't load your requests.</p>
        <p className="text-[12.5px] text-muted">{inbox.error.message}</p>
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
  } else {
    list = (
      <InboxList
        inbox={inbox.data}
        selectedThreadId={threadId}
        seen={seen}
        orgSlug={session.data?.member.org.slug}
        onOpen={onOpenThread}
      />
    );
  }

  return (
    <div className="@container/inbox flex h-full min-h-0 w-full bg-canvas text-ink">
      <aside
        aria-label="Requests list"
        className={cx(
          "flex min-h-0 w-full shrink-0 flex-col border-r border-border @2xl/inbox:w-[320px]",
          threadId !== undefined && "hidden @2xl/inbox:flex",
        )}
      >
        {list}
      </aside>
      <main className={cx("min-h-0 min-w-0 flex-1", threadId === undefined ? "hidden @2xl/inbox:flex" : "flex")}>
        {threadId === undefined ? (
          <NothingSelected inbox={inbox.data} onOpen={onOpenThread} />
        ) : (
          <ThreadPane
            key={threadId}
            threadId={threadId}
            memberId={memberId}
            item={findItem(inbox.data, threadId)}
            onBack={onCloseThread}
            onSeen={markSeen}
          />
        )}
      </main>
    </div>
  );
}

function NothingSelected({ inbox, onOpen }: { inbox: Inbox | undefined; onOpen: (threadId: string) => void }) {
  const first = inbox?.needs_you[0];
  const needs = inbox?.needs_you.length ?? 0;
  const handling = inbox?.claude_handling.length ?? 0;
  return (
    <div className="flex flex-1 flex-col items-center justify-center px-8 text-center">
      <span className="flex size-11 items-center justify-center rounded-2xl border border-border bg-surface text-muted shadow-card">
        <InboxIcon size={20} strokeWidth={1.5} />
      </span>
      <p className="mt-4 font-serif text-[22px] tracking-[-0.01em] text-ink">
        {needs > 0 ? `${String(needs)} ${needs === 1 ? "request needs" : "requests need"} you` : "Nothing needs you"}
      </p>
      <p className="mt-1.5 max-w-[340px] text-[13.5px] leading-5 text-muted">
        {handling > 0
          ? `Claude is handling ${String(handling)} more on its own. Open one to see where it stands.`
          : "When another organization's Claude asks for something only your team can answer, it lands here."}
      </p>
      {first !== undefined && (
        <Button
          className="mt-5"
          variant="primary"
          onClick={() => {
            onOpen(first.thread_id);
          }}
        >
          Open “{first.title.length > 42 ? `${first.title.slice(0, 40).trimEnd()}…` : first.title}”
        </Button>
      )}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading requests" className="flex flex-col gap-1 px-2 pt-[60px]">
      {[0.8, 0.65, 0.72, 0.55].map((width) => (
        <div key={width} className="flex gap-3 px-2.5 py-2.5">
          <span className="size-[30px] shrink-0 animate-pulse rounded-[8px] bg-hover" />
          <span className="flex flex-1 flex-col gap-2 pt-1">
            <span className="h-3 animate-pulse rounded bg-hover" style={{ width: `${String(width * 100)}%` }} />
            <span className="h-2.5 w-1/2 animate-pulse rounded bg-hover" />
          </span>
        </div>
      ))}
    </div>
  );
}
