import { Send } from "lucide-react";
import { useId } from "react";

import { cx } from "../../shell/ui/cx";
import { screenedLine } from "../brief";
import { Markdown } from "../Markdown";
import { FitBadge, OrgAvatar, Pill, ToolCard } from "../parts";
import type { OutreachCardItem } from "../types";

function StatusMeta({ item }: { item: OutreachCardItem }) {
  switch (item.status) {
    case "pending":
      return <Pill tone="accent">Waiting for you</Pill>;
    case "approved":
      return <Pill tone="success">Sent</Pill>;
    case "rejected":
      return <Pill tone="muted">Not sent</Pill>;
    case "superseded":
      return <Pill tone="muted">Replaced</Pill>;
  }
}

/**
 * The outreach card as it stays in the transcript: the brief Claude wrote (the member's
 * version, once approved with edits) and who it goes to, with fit and reason. While it
 * waits on the member, the ask-user card above the composer asks the question.
 */
export function OutreachCard({ item }: { item: OutreachCardItem }) {
  const titleId = useId();
  return (
    <ToolCard
      labelledBy={titleId}
      icon={<Send size={16} strokeWidth={1.6} />}
      title="Network request"
      meta={<StatusMeta item={item} />}
    >
      <Markdown size="sm" className="chat-prose-lead">
        {item.brief}
      </Markdown>
      <ul className="mt-2 divide-y divide-border/70 border-t border-border/70">
        {item.recipients.map((recipient) => (
          <li
            key={recipient.org.id}
            className={cx("flex items-start gap-3 py-2.5", !recipient.selected && "opacity-60")}
          >
            <OrgAvatar org={recipient.org} size={22} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className={cx("text-[14px] text-ink", !recipient.selected && "line-through")}>
                  {recipient.org.name}
                </span>
                <FitBadge fit={recipient.fit} />
                {!recipient.selected && item.status === "approved" && (
                  <span className="text-[12px] text-muted">Not sent</span>
                )}
              </div>
              <p className="mt-0.5 text-[13px] leading-5 text-muted">{recipient.reason}</p>
            </div>
          </li>
        ))}
      </ul>
      {item.screened !== null && <p className="mt-1 text-[12.5px] text-muted">{screenedLine(item.screened)}</p>}
      {item.status === "rejected" && item.feedback !== null && (
        <p className="mt-3 rounded-control bg-bubble px-3 py-2 text-[13.5px] leading-5 text-secondary">
          You asked Claude to change it: “{item.feedback}”
        </p>
      )}
    </ToolCard>
  );
}
