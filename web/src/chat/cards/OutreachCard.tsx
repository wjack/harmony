import { Send } from "lucide-react";
import { useId } from "react";

import { cx } from "../../shell/ui/cx";
import { screenedLine } from "../brief";
import { Markdown } from "../Markdown";
import { FitBadge, OrgAvatar, Pill, ToolCard } from "../parts";
import type { OutreachCardItem } from "../types";
import { RecipientAsk } from "./RecipientAsk";

function StatusMeta({ item }: { item: OutreachCardItem }) {
  // A card the chat sent without review never waits on the member (LIVE_DEMO_PLAN 3.10).
  if (item.auto) {
    switch (item.status) {
      case "pending":
        return (
          <Pill tone="info" pulse>
            Sending…
          </Pill>
        );
      case "approved":
        return <Pill tone="success">Sent without review</Pill>;
      case "rejected":
      case "superseded":
        return <Pill tone="muted">Not sent</Pill>;
    }
  }
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

/** Why it didn't go out, under the recipients: the member's feedback, or why an unreviewed send failed. */
function NotSentNote({ item }: { item: OutreachCardItem }) {
  if (item.status !== "rejected" || item.feedback === null) return null;
  return (
    <p className="mt-3 rounded-control bg-bubble px-3 py-2 text-[13.5px] leading-5 text-secondary">
      {item.auto ? `Not sent: ${item.feedback}` : `You asked Claude to change it: “${item.feedback}”`}
    </p>
  );
}

/**
 * The outreach card as it stays in the transcript: the request's title, the brief Claude
 * wrote (the member's version, once approved with edits), and who it goes to, with fit
 * and reason, and what each is asked. While it waits on the member, the ask-user card
 * above the composer asks the question; one the chat sends without review has nothing
 * to ask.
 */
export function OutreachCard({ item }: { item: OutreachCardItem }) {
  const titleId = useId();
  // Approved, or going out without review: rows left out of the request read "Not sent".
  const decided = item.status === "approved" || (item.auto && item.status === "pending");
  return (
    <ToolCard
      labelledBy={titleId}
      icon={<Send size={16} strokeWidth={1.6} />}
      title="Harmony Network Request"
      meta={<StatusMeta item={item} />}
    >
      {item.title && <p className="mb-1.5 text-[15px] leading-6 font-medium text-ink">{item.title}</p>}
      <Markdown size="sm" className="chat-prose-lead">
        {item.brief}
      </Markdown>
      <ul className="mt-2 divide-y divide-border/70 border-t border-border/70">
        {item.recipients.map((recipient) => (
          <li key={recipient.org.id} className={cx("flex items-start gap-3 py-2.5", !recipient.selected && "opacity-60")}>
            <OrgAvatar org={recipient.org} size={22} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className={cx("text-[14px] text-ink", !recipient.selected && "line-through")}>
                  {recipient.org.name}
                </span>
                <FitBadge fit={recipient.fit} />
                {!recipient.selected && decided && <span className="text-[12px] text-muted">Not sent</span>}
              </div>
              <p className="mt-0.5 text-[13px] leading-5 text-muted">{recipient.reason}</p>
              <RecipientAsk title={recipient.title} ask={recipient.ask} className="mt-2" />
            </div>
          </li>
        ))}
      </ul>
      {item.screened !== null && <p className="mt-1 text-[12.5px] text-muted">{screenedLine(item.screened)}</p>}
      <NotSentNote item={item} />
    </ToolCard>
  );
}
